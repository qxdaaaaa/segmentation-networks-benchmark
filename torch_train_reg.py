import argparse
import os.path
import sys

import numpy as np
import pandas as pd
import torch
from torch import nn
from tensorboardX import SummaryWriter
from torch.backends import cudnn
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from torchvision.utils import make_grid
from tqdm import tqdm

from lib.datasets.Inria import INRIA
from lib.datasets.dsb2018 import DSB2018Sliced
from lib.datasets.shapes import SHAPES
from lib.losses import JaccardLoss, FocalLossBinary, BCEWithLogitsLossAndSmoothJaccard, BCEWithSigmoidLoss
from lib.metrics import JaccardScore, PixelAccuracy
from lib.models import linknet, unet16, unet11
from lib.models.dilated_linknet import DilatedLinkNet34
from lib.models.duc_hdc import ResNetDUCHDC, ResNetDUC
from lib.models.gcn152 import GCN152, GCN34
from lib.models.linknext import LinkNext
from lib.models.psp_net import PSPNet
from lib.models.tiramisu import FCDenseNet67
from lib.models.unet import UNet
from lib.models.unet_abn import UNetABN
from lib.models.zf_unet import ZF_UNET
from lib.train_utils import AverageMeter, PRCurveMeter
from lib.common import count_parameters
from torch.nn.modules.loss import _Loss

from torch_train import get_model, get_loss, get_optimizer, get_dataset

tqdm.monitor_interval = 0  # Workaround for https://github.com/tqdm/tqdm/issues/481


class Conv2dRegularization(_Loss):
    def __init__(self, l1_factor=0.0005, l2_factor=0.0005):
        super(Conv2dRegularization, self).__init__()
        self.l1_factor = l1_factor
        self.l2_factor = l2_factor
        self.l1_crit = nn.L1Loss(size_average=False)
        self.l2_crit = nn.MSELoss(size_average=False)

    def forward(self, model):
        reg_loss_l1 = 0
        reg_loss_l2 = 0

        for module in model.modules():
            if isinstance(module, nn.Conv2d):
                if module.weight.requires_grad:
                    # We apply L1 norml to weights in order to make kernel sparse
                    reg_loss_l1 += self.l1_crit(module.weight, target=torch.zeros_like(module.weight))

                    # We apply L2 norm to bias in order to make them zero-mean
                    if module.bias is not None:
                        reg_loss_l2 += self.l2_crit(module.bias, target=torch.zeros_like(module.bias))

        return self.l1_factor * reg_loss_l1, self.l2_factor * reg_loss_l2


def train(model, loss, optimizer, dataloader, epoch: int, metrics={}, summary_writer=None):
    losses = AverageMeter()

    train_scores = {}
    for key, _ in metrics.items():
        train_scores[key] = AverageMeter()

    conv2d_reg = Conv2dRegularization().cuda()

    with torch.set_grad_enabled(True):
        model.train()
        n_batches = len(dataloader)
        with tqdm(total=n_batches) as tq:
            tq.set_description('Train')
            x = None
            y = None
            outputs = None
            batch_loss = None

            for batch_index, (x, y) in enumerate(dataloader):
                x, y = x.cuda(non_blocking=True), y.cuda(non_blocking=True)

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward + backward + optimize
                outputs = model(x)
                l1_penalty, l2_penalty = conv2d_reg(model)

                batch_loss = loss(outputs, y)

                batch_size = x.size(0)
                (batch_size * batch_loss + l1_penalty + l2_penalty).backward()

                optimizer.step()

                # Batch train end
                # Log train progress

                l1_penalty = l1_penalty.cpu().item()
                l2_penalty = l2_penalty.cpu().item()


                batch_loss_val = batch_loss.cpu().item()
                if summary_writer is not None:
                    summary_writer.add_scalar('train/batch/loss', batch_loss_val, epoch * n_batches + batch_index)
                    summary_writer.add_scalar('train/batch/l1_penalty', l1_penalty, epoch * n_batches + batch_index)
                    summary_writer.add_scalar('train/batch/l2_penalty', l2_penalty, epoch * n_batches + batch_index)

                    # Plot gradient absmax and absmin to see if there are any gradient explosions
                    grad_max = 0
                    for name, param in model.named_parameters():
                        if param.grad is not None:
                            grad_max = max(grad_max, param.grad.abs().max().cpu().item())

                    summary_writer.add_scalar('train/grad/global_abs_max', grad_max, epoch * n_batches + batch_index)

                losses.update(batch_loss_val)

                for key, metric in metrics.items():
                    score = metric(outputs, y).cpu().item()
                    train_scores[key].update(score)

                    if summary_writer is not None:
                        summary_writer.add_scalar('train/batch/' + key, score, epoch * n_batches + batch_index)

                tq.set_postfix(loss='{:.3f}'.format(losses.avg),
                               l1_penalty='{:.3f}'.format(l1_penalty),
                               l2_penalty='{:.3f}'.format(l2_penalty), **train_scores)
                tq.update()

            # End of train epoch
            if summary_writer is not None:
                summary_writer.add_image('train/image', make_grid(x.cpu(), normalize=True), epoch)
                summary_writer.add_image('train/y_true', make_grid(y.cpu(), normalize=True), epoch)
                summary_writer.add_image('train/y_pred', make_grid(outputs.sigmoid().cpu(), normalize=True), epoch)
                summary_writer.add_scalar('train/epoch/loss', losses.avg, epoch)
                for key, value in train_scores.items():
                    summary_writer.add_scalar('train/epoch/' + key, value.avg, epoch)

                # Plot histogram of parameters after each epoch
                for name, param in model.named_parameters():
                    if param.grad is not None:
                        # Plot weighs
                        param_data = param.data.cpu().numpy()
                        summary_writer.add_histogram('model/' + name, param_data, epoch, bins='doane')

            del x, y, outputs, batch_loss

    return losses, train_scores


def validate(model, loss, dataloader, epoch: int, metrics=dict(), summary_writer: SummaryWriter = None):
    losses = AverageMeter()
    pr_meter = PRCurveMeter()

    valid_scores = {}
    for key, _ in metrics.items():
        valid_scores[key] = AverageMeter()

    with torch.set_grad_enabled(False):
        model.eval()

        n_batches = len(dataloader)
        with tqdm(total=len(dataloader)) as tq:
            tq.set_description('Validation')

            x = None
            y = None
            outputs = None
            batch_loss = None

            for batch_index, (x, y) in enumerate(dataloader):
                x, y = x.cuda(non_blocking=True), y.cuda(non_blocking=True)

                # forward + backward + optimize
                outputs = model(x)
                batch_loss = loss(outputs, y)

                # Log train progress

                batch_loss_val = batch_loss.cpu().item()
                if summary_writer is not None:
                    summary_writer.add_scalar('val/batch/loss', batch_loss_val, epoch * n_batches + batch_index)

                losses.update(batch_loss_val)

                for key, metric in metrics.items():
                    score = metric(outputs, y).cpu().item()
                    valid_scores[key].update(score)

                    if summary_writer is not None:
                        summary_writer.add_scalar('val/batch/' + key, score, epoch * n_batches + batch_index)

                tq.set_postfix(loss='{:.3f}'.format(losses.avg), **valid_scores)
                tq.update()

            if summary_writer is not None:
                summary_writer.add_image('val/image', make_grid(x.cpu(), normalize=True), epoch)
                summary_writer.add_image('val/y_true', make_grid(y.cpu(), normalize=True), epoch)
                summary_writer.add_image('val/y_pred', make_grid(outputs.sigmoid().cpu(), normalize=True), epoch)
                summary_writer.add_scalar('val/epoch/loss', losses.avg, epoch)
                for key, value in valid_scores.items():
                    summary_writer.add_scalar('val/epoch/' + key, value.avg, epoch)

                # Compute PR curve only for last batch, because computing it for entire validation set is costly
                pr_meter.update(outputs, y)
                summary_writer.add_pr_curve_raw('val/pr_curve',
                                                true_positive_counts=pr_meter.tp,
                                                true_negative_counts=pr_meter.tn,
                                                false_negative_counts=pr_meter.fn,
                                                false_positive_counts=pr_meter.fp,
                                                precision=pr_meter.precision(),
                                                recall=pr_meter.recall(),
                                                global_step=epoch)
            del x, y, outputs, batch_loss

    return losses, valid_scores


def save_snapshot(model: nn.Module, optimizer: Optimizer, loss: float, epoch: int, train_history: pd.DataFrame, snapshot_file: str):
    torch.save({
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'epoch': epoch,
        'loss': loss,
        'train_history': train_history.to_dict(),
        'args': ' '.join(sys.argv[1:])
    }, snapshot_file)


def restore_snapshot(model: nn.Module, optimizer: Optimizer, snapshot_file: str):
    checkpoint = torch.load(snapshot_file)
    start_epoch = checkpoint['epoch'] + 1
    best_loss = checkpoint['loss']
    model.load_state_dict(checkpoint['model'])

    if optimizer is not None:
        optimizer.load_state_dict(checkpoint['optimizer'])

    train_history = pd.DataFrame.from_dict(checkpoint['train_history'])

    return start_epoch, train_history, best_loss


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument('-g', '--grayscale', action='store_true', help='Whether to use grayscale image instead of RGB')
    parser.add_argument('-m', '--model', required=True, type=str, help='Name of the model')
    parser.add_argument('-p', '--patch-size', type=int, default=224)
    parser.add_argument('-b', '--batch-size', type=int, default=1, help='Batch Size during training, e.g. -b 64')
    parser.add_argument('-lr', '--learning-rate', type=float, default=1e-3, help='Initial learning rate')
    parser.add_argument('-l', '--loss', type=str, default='bce', help='Target loss')
    parser.add_argument('-o', '--optimizer', default='SGD', help='Name of the optimizer')
    parser.add_argument('-e', '--epochs', type=int, default=100, help='Epoch to run')
    parser.add_argument('-d', '--dataset', type=str, help='Name of the dataset to use for training.')
    parser.add_argument('-dd', '--data-dir', type=str, default='data', help='Root directory where datasets are located.')
    parser.add_argument('-s', '--steps', type=int, default=128, help='Steps per epoch')
    parser.add_argument('-x', '--experiment', type=str, help='Name of the experiment')
    parser.add_argument('-w', '--workers', default=0, type=int, help='Num workers')
    parser.add_argument('-r', '--resume', action='store_true')
    parser.add_argument('-mem', '--memory', action='store_true')

    args = parser.parse_args()
    cudnn.benchmark = True

    if args.experiment is None:
        args.experiment = '%s_%s_reg_%d_%s_%s' % (args.dataset, args.model, args.patch_size, 'gray' if args.grayscale else 'rgb', args.loss)

    experiment_dir = os.path.join('experiments', args.dataset, args.loss, args.experiment)
    os.makedirs(experiment_dir, exist_ok=True)

    writer = SummaryWriter(comment='_' + args.experiment)

    with open(os.path.join(experiment_dir, 'arguments.txt'), 'w') as f:
        f.write(' '.join(sys.argv[1:]))

    model = get_model(args.model, patch_size=args.patch_size, num_channels=1 if args.grayscale else 3)

    # Write model graph
    dummy_input = torch.autograd.Variable(torch.rand((args.batch_size, 1 if args.grayscale else 3, args.patch_size, args.patch_size)))
    writer.add_graph(model, dummy_input)

    model = model.cuda()
    loss = get_loss(args.loss).cuda()
    optimizer = get_optimizer(args.optimizer, model.parameters(), args.learning_rate)
    metrics = {'iou': JaccardScore().cuda(), 'accuracy': PixelAccuracy().cuda()}

    trainset, validset, num_classes = get_dataset(args.dataset, args.data_dir, grayscale=args.grayscale, patch_size=args.patch_size, keep_in_mem=args.memory)
    print('Train set size', len(trainset))
    print('Valid set size', len(validset))
    print('Model         ', model)
    print('Parameters    ', count_parameters(model))

    trainloader = DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers, pin_memory=True, drop_last=True)
    validloader = DataLoader(validset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers, pin_memory=True, drop_last=True)

    start_epoch = 0
    best_loss = np.inf
    train_history = pd.DataFrame()

    # Checkpoint is train result of epoch with best loss
    checkpoint_filename = os.path.join(experiment_dir, f'{args.model}_checkpoint.pth')

    # Snapshot is train result of last epoch
    snapshot_filename = os.path.join(experiment_dir, f'{args.model}_snapshot.pth')

    if args.resume:
        start_epoch, train_history, best_loss = restore_snapshot(model, optimizer, checkpoint_filename)
        print('Resuming training from epoch', start_epoch, ' and loss', best_loss)
        print(train_history)

    for epoch in range(start_epoch, args.epochs):
        train_loss, train_scores = train(model, loss, optimizer, trainloader, epoch, metrics, summary_writer=writer)
        valid_loss, valid_scores = validate(model, loss, validloader, epoch, metrics, summary_writer=writer)

        summary = {
            'epoch': [epoch],
            'loss': [train_loss.avg],
            'val_loss': [valid_loss.avg]
        }

        for key, value in train_scores.items():
            summary[key] = [value.avg]

        for key, value in valid_scores.items():
            summary['val_' + key] = [value.avg]

        train_history = train_history.append(pd.DataFrame.from_dict(summary), ignore_index=True)

        print(epoch, summary)

        if valid_loss.avg < best_loss:
            save_snapshot(model, optimizer, valid_loss.avg, epoch, train_history, checkpoint_filename)
            best_loss = valid_loss.avg
            print('Checkpoint saved', epoch, best_loss)

        save_snapshot(model, optimizer, valid_loss.avg, epoch, train_history, snapshot_filename)

    print('Training is finished...')

    train_history.to_csv(os.path.join(experiment_dir, args.experiment + '.csv'),
                         index=False,
                         mode='a' if args.resume else 'w',
                         header=not args.resume)


if __name__ == '__main__':
    main()
