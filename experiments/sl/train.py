#!/usr/bin/env python3
import logging
import os
import random
import shutil


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import hydra
import omegaconf
import src
import torch
import wandb
from experiments.sl.configs.schema import TrainConfig
from experiments.sl.utils import (
    compute_metrics,
    EarlyStopper,
    set_seed_everywhere,
    train_val_split,
)
from hydra.utils import get_original_cwd
from torch.utils.data import DataLoader
from tqdm import tqdm


def checkpoint(
    sfr: src.SFR, optimizer: torch.optim.Optimizer, save_dir: str, verbose: bool = False
):
    if verbose:
        logger.info("Saving SFR and optimiser...")
    state = {"model": sfr.state_dict(), "optimizer": optimizer.state_dict()}
    fname = "best_ckpt_dict.pt"
    save_name = os.path.join(save_dir, fname)
    torch.save(state, save_name)
    if verbose:
        logger.info("Finished saving model and optimiser etc")
    return save_name


@torch.no_grad()
def predict_probs(
    dataloader: DataLoader, network: torch.nn.Module, device: str = "cpu"
):
    py = []
    for x, _ in dataloader:
        py.append(torch.softmax(network(x.to(device)), dim=-1))

    return torch.cat(py).cpu().numpy()


@hydra.main(version_base="1.3", config_path="./configs", config_name="train")
def train(cfg: TrainConfig):
    try:  # Make experiment reproducible
        set_seed_everywhere(cfg.random_seed)
    except:
        random_seed = random.randint(0, 10000)
        set_seed_everywhere(random_seed)

    # if "cuda" in cfg.device:
    #     cfg.device = "cuda" if torch.cuda.is_available() else "cpu"

    if cfg.double:
        logger.info("Using float64")
        torch.set_default_dtype(torch.double)

    # Ensure that all operations are deterministic on GPU (if used) for reproducibility
    eval('setattr(torch.backends.cudnn, "determinstic", True)')
    eval('setattr(torch.backends.cudnn, "benchmark", False)')

    cfg.device = "cuda" if torch.cuda.is_available() else "cpu"
    print("Using device: {}".format(cfg.device))

    # Load the data with train/val/test split
    ds_train, ds_val, ds_test, _ = hydra.utils.instantiate(
        cfg.dataset, dir=os.path.join(get_original_cwd(), "data")
    )
    cfg.output_dim = ds_train.output_dim
    print(f"output_dim: {cfg.output_dim}")
    print(cfg.dataset.name)
    # print(ds_train)

    # Instantiate the neural network
    network = hydra.utils.instantiate(cfg.network, ds_train=ds_train)

    # Create data loaders
    train_loader = DataLoader(dataset=ds_train, shuffle=True, batch_size=cfg.batch_size)
    val_loader = DataLoader(dataset=ds_val, shuffle=False, batch_size=cfg.batch_size)
    test_loader = DataLoader(ds_test, batch_size=cfg.batch_size, shuffle=True)

    # Instantiate SFR
    sfr = hydra.utils.instantiate(cfg.sfr, model=network)

    # Initialise WandB
    if cfg.wandb.use_wandb:
        run = wandb.init(
            project=cfg.wandb.project,
            name=cfg.wandb.run_name,
            group=cfg.wandb.group,
            tags=cfg.wandb.tags,
            config=omegaconf.OmegaConf.to_container(
                cfg, resolve=True, throw_on_missing=True
            ),
            dir=get_original_cwd(),  # don't nest wandb inside hydra dir
        )
        # print("path")
        # print("get_original_cwd() {}".format(get_original_cwd()))
        # print("os.path.abspath('.hydra') {}".format(os.path.abspath(".hydra")))
        # print("os.getcwd() {}".format(os.getcwd()))
        # Save hydra configs with wandb (handles hydra's multirun dir)
        try:
            shutil.copytree(
                os.path.abspath(".hydra"),
                os.path.join(os.path.join(get_original_cwd(), wandb.run.dir), "hydra"),
            )
            wandb.save("hydra")
        except FileExistsError:
            pass

    optimizer = torch.optim.Adam([{"params": sfr.parameters()}], lr=cfg.lr)

    @torch.no_grad()
    def map_pred_fn(x, idx=None):
        f = sfr.network(x.to(cfg.device))
        return sfr.likelihood.inv_link(f)
        # return torch.softmax(sfr.network(x.to(cfg.device)), dim=-1)

    @torch.no_grad()
    def loss_fn(data_loader: DataLoader):
        cum_loss = 0
        for X, y in data_loader:
            X, y = X.to(cfg.device), y.to(cfg.device)
            loss = sfr.loss(X, y)
            cum_loss += loss
        return cum_loss

    early_stopper = EarlyStopper(
        patience=int(cfg.early_stop.patience / cfg.logging_epoch_freq),
        min_prior_precision=cfg.early_stop.min_prior_precision,
    )

    best_nll = float("inf")
    for epoch in tqdm(list(range(cfg.n_epochs))):
        for X, y in train_loader:
            X, y = X.to(cfg.device), y.to(cfg.device)
            loss = sfr.loss(X, y)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            wandb.log({"loss": loss})

        if epoch % cfg.logging_epoch_freq == 0:
            val_loss = loss_fn(val_loader)
            wandb.log({"val_loss": val_loss})
            train_metrics = compute_metrics(
                pred_fn=map_pred_fn,
                data_loader=train_loader,
                # ds_test=ds_train,
                # batch_size=cfg.batch_size,
                device=cfg.device,
            )
            val_metrics = compute_metrics(
                pred_fn=map_pred_fn,
                data_loader=val_loader,
                # ds_test=ds_val,
                # batch_size=cfg.batch_size,
                device=cfg.device,
            )
            test_metrics = compute_metrics(
                pred_fn=map_pred_fn,
                data_loader=test_loader,
                # ds_test=ds_test,
                # batch_size=cfg.batch_size,
                device=cfg.device,
            )
            wandb.log({"train/": train_metrics})
            wandb.log({"val/": val_metrics})
            wandb.log({"test/": test_metrics})
            wandb.log({"epoch": epoch})

            if val_metrics["nll"] < best_nll:
                checkpoint(sfr=sfr, optimizer=optimizer, save_dir=run.dir)
                best_nll = val_metrics["nll"]
                wandb.log({"best_test/": test_metrics})
                wandb.log({"best_val/": val_metrics})
            if early_stopper(val_metrics["nll"]):  # (val_loss):
                logger.info("Early stopping criteria met, stopping training...")
                break

    logger.info("Finished training")

    # state = {"model": sfr.state_dict(), "optimizer": optimizer.state_dict()}

    # logger.info("Saving model and optimiser etc...")
    # fname = "ckpt_dict.pt"
    # torch.save(state, os.path.join(run.dir, fname))
    # logger.info("Finished saving model and optimiser etc")
    # return run.dir
    return sfr


if __name__ == "__main__":
    train()  # pyright: ignore
    # train_on_cluster()  # pyright: ignore
