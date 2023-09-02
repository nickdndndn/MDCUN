from pathlib import Path
from tqdm import tqdm

import torch
from torch.optim import Adam
from torch.nn import L1Loss
from torch.utils.data import DataLoader
from torchvision.transforms import Resize, RandomHorizontalFlip, RandomVerticalFlip, RandomRotation
from torchmetrics import MetricCollection, PeakSignalNoiseRatio, StructuralSimilarityIndexMeasure
from torch.optim.lr_scheduler import StepLR
from torchinfo import summary

from data_loader.DataLoader import DIV2K, GaoFen2, Sev2Mod, WV3, GaoFen2panformer
from models.MDCUN import MDCUN
from utils import *


def main():
    # Prepare device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(device)

    # Initialize DataLoader
    train_dataset = WV3(
        Path("/home/ubuntu/project/Data/WorldView3/train/train_wv3-001.h5"))  # transforms=[(RandomHorizontalFlip(1), 0.3), (RandomVerticalFlip(1), 0.3)]
    train_loader = DataLoader(
        dataset=train_dataset, batch_size=4, shuffle=True, drop_last=True)

    validation_dataset = WV3(
        Path("/home/ubuntu/project/Data/WorldView3/val/valid_wv3.h5"))
    validation_loader = DataLoader(
        dataset=validation_dataset, batch_size=1, shuffle=True)

    test_dataset = WV3(
        Path("/home/ubuntu/project/Data/WorldView3/drive-download-20230627T115841Z-001/test_wv3_multiExm1.h5"))
    test_loader = DataLoader(
        dataset=test_dataset, batch_size=1, shuffle=False)

    # Initialize Model, optimizer, criterion and metrics
    model = MDCUN(ms_channels= 8, T=1, mslr_mean=train_dataset.mslr_mean.to(device), mslr_std=train_dataset.mslr_std.to(device), pan_mean=train_dataset.pan_mean.to(device),
                  pan_std=train_dataset.pan_std.to(device)).to(device)

    optimizer = Adam(model.parameters(), lr=5e-4)

    criterion = L1Loss().to(device)

    metric_collection = MetricCollection({
        'psnr': PeakSignalNoiseRatio().to(device),
        'ssim': StructuralSimilarityIndexMeasure().to(device)
    })

    val_metric_collection = MetricCollection({
        'psnr': PeakSignalNoiseRatio().to(device),
        'ssim': StructuralSimilarityIndexMeasure().to(device)
    })

    test_metric_collection = MetricCollection({
        'psnr': PeakSignalNoiseRatio().to(device),
        'ssim': StructuralSimilarityIndexMeasure().to(device)
    })

    tr_report_loss = 0
    val_report_loss = 0
    test_report_loss = 0
    tr_metrics = []
    val_metrics = []
    test_metrics = []
    best_eval_psnr = 0
    best_test_psnr = 0
    current_daytime = datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
    steps = 600000
    save_interval = 1000
    report_interval = 50
    test_intervals = [100000, 200000, 300000,
                      400000,500000, 600000]
    evaluation_interval = [100000, 200000, 300000,
                      400000,500000, 600000]

    val_steps = 100

    # Model summary
    summary(model, [(1, 1, 256, 256), (1, 8, 64, 64)],
            dtypes=[torch.float32, torch.float32])

    scheduler = StepLR(optimizer, step_size=1, gamma=0.5)
    lr_decay_intervals = 150000

    print('==> Starting training ...')
    train_iter = iter(train_loader)
    train_progress_bar = tqdm(iter(range(steps)), total=steps, desc="Training",
                              leave=False, bar_format='{desc:<8}{percentage:3.0f}%|{bar:15}{r_bar}')
    for step in train_progress_bar:
        if step % save_interval == 0 and step != 0:
            checkpoint = {'step': step,
                          'state_dict': model.state_dict(),
                          'optimizer': optimizer.state_dict(),
                          'tr_metrics': tr_metrics,
                          'val_metrics': val_metrics,
                          'test_metrics': test_metrics}
            save_checkpoint(checkpoint, 'MDCUN', current_daytime)

        try:
            # Samples the batch
            pan, mslr, mshr = next(train_iter)
        except StopIteration:
            # restart the loader if the previous loader is exhausted.
            train_iter = iter(train_loader)
            pan, mslr, mshr = next(train_iter)

        # forward
        pan, mslr, mshr = pan.to(device), mslr.to(device), mshr.to(device)
        mssr = model(pan, mslr)
        tr_loss = criterion(mssr, mshr)
        tr_report_loss += tr_loss
        batch_metric = metric_collection.forward(mssr, mshr)

        # backward
        optimizer.zero_grad()
        tr_loss.backward()
        optimizer.step()

        batch_metrics = {'loss': tr_loss.item(),
                         'psnr': batch_metric['psnr'].item(),
                         'ssim': batch_metric['ssim'].item()}

        # report metrics
        train_progress_bar.set_postfix(
            loss=batch_metrics["loss"], psnr=f'{batch_metrics["psnr"]:.4f}', ssim=f'{batch_metrics["ssim"]:.4f}')

        # lr_decay
        if step % lr_decay_intervals == 0 and step != 0:
            scheduler.step()
            # print(scheduler.get_last_lr())

        # Store metrics
        if (step + 1) % report_interval == 0 and step != 0:
            # Batch metrics
            tr_report_loss = tr_report_loss / (report_interval)
            tr_metric = metric_collection.compute()

            # store metrics
            tr_metrics.append({'loss': tr_report_loss.item(),
                               'psnr': tr_metric['psnr'].item(),
                               'ssim': tr_metric['ssim'].item()})

            # reset metrics
            tr_report_loss = 0
            metric_collection.reset()

        # Evaluate model
        if (step + 1) in evaluation_interval and step != 0:
            # evaluation mode
            model.eval()
            with torch.no_grad():
                print("\n==> Start evaluating ...")
                val_steps = val_steps if val_steps else len(validation_loader)
                eval_progress_bar = tqdm(iter(range(val_steps)), total=val_steps, desc="Validation",
                                         leave=False, bar_format='{desc:<8}{percentage:3.0f}%|{bar:15}{r_bar}')
                val_iter = iter(validation_loader)
                for eval_step in eval_progress_bar:
                    try:
                        # Samples the batch
                        pan, mslr, mshr = next(val_iter)
                    except StopIteration:
                        # restart the loader if the previous loader is exhausted.
                        val_iter = iter(validation_loader)
                        pan, mslr, mshr = next(val_iter)
                    # forward
                    pan, mslr, mshr = pan.to(device), mslr.to(
                        device), mshr.to(device)
                    mssr = model(pan, mslr)
                    val_loss = criterion(mssr, mshr)
                    val_metric = val_metric_collection.forward(mssr, mshr)
                    val_report_loss += val_loss

                    # report metrics
                    eval_progress_bar.set_postfix(
                        loss=f'{val_loss.item()}', psnr=f'{val_metric["psnr"].item():.2f}', ssim=f'{val_metric["ssim"].item():.2f}')

                # compute metrics total
                val_report_loss = val_report_loss / len(validation_loader)
                val_metric = val_metric_collection.compute()
                val_metrics.append({'loss': val_report_loss.item(),
                                    'psnr': val_metric['psnr'].item(),
                                    'ssim': val_metric['ssim'].item()})

                print(
                    f'\nEvaluation: avg_loss = {val_report_loss.item():.4f} , avg_psnr= {val_metric["psnr"]:.4f}, avg_ssim={val_metric["ssim"]:.4f}')

                # reset metrics
                val_report_loss = 0
                val_metric_collection.reset()
                print("==> End evaluating <==\n")

            # train mode
            model.train()

            # save best evaluation model based on PSNR
            if val_metrics[-1]['psnr'] > best_eval_psnr:
                best_eval_psnr = val_metrics[-1]['psnr']
                checkpoint = {'step': step,
                              'state_dict': model.state_dict(),
                              'optimizer': optimizer.state_dict(),
                              'tr_metrics': tr_metrics,
                              'val_metrics': val_metrics,
                              'test_metrics': test_metrics}
                save_checkpoint(checkpoint, 'MDCUN',
                                current_daytime + '_best_eval')

        # test model
        if (step + 1) in test_intervals and step != 0:
            # evaluation mode
            model.eval()
            with torch.no_grad():
                print("\n==> Start testing ...")
                test_progress_bar = tqdm(iter(test_loader), total=len(
                    test_loader), desc="Testing", leave=False, bar_format='{desc:<8}{percentage:3.0f}%|{bar:15}{r_bar}')
                for pan, mslr, mshr in test_progress_bar:
                    # forward
                    pan, mslr, mshr = pan.to(device), mslr.to(
                        device), mshr.to(device)
                    mssr = model(pan, mslr)
                    test_loss = criterion(mssr, mshr)
                    test_metric = test_metric_collection.forward(mssr, mshr)
                    test_report_loss += test_loss

                    # report metrics
                    test_progress_bar.set_postfix(
                        loss=f'{test_loss.item()}', psnr=f'{test_metric["psnr"].item():.2f}', ssim=f'{test_metric["ssim"].item():.2f}')

                # compute metrics total
                test_report_loss = test_report_loss / len(test_loader)
                test_metric = test_metric_collection.compute()
                test_metrics.append({'loss': test_report_loss.item(),
                                     'psnr': test_metric['psnr'].item(),
                                     'ssim': test_metric['ssim'].item()})

                print(
                    f'\nTesting: avg_loss = {test_report_loss.item():.4f} , avg_psnr= {test_metric["psnr"]:.4f}, avg_ssim={test_metric["ssim"]:.4f}')

                # reset metrics
                test_report_loss = 0
                test_metric_collection.reset()
                print("==> End testing <==\n")

            # train mode
            model.train()

            # save best test model based on PSNR
            if test_metrics[-1]['psnr'] > best_test_psnr:
                best_test_psnr = test_metrics[-1]['psnr']
                checkpoint = {'step': step,
                              'state_dict': model.state_dict(),
                              'optimizer': optimizer.state_dict(),
                              'tr_metrics': tr_metrics,
                              # 'val_metrics': val_metrics,
                              'test_metrics': test_metrics}
                save_checkpoint(checkpoint, 'MDCUN',
                                current_daytime + '_best_test')

    print('==> training ended <==')


if __name__ == '__main__':
    main()
