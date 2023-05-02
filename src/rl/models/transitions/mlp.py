#!/usr/bin/env python3
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import torch
import wandb
from src.rl.custom_types import Action, InputData, OutputData, State, StatePrediction
from src.rl.utils import EarlyStopper
from torchrl.data import ReplayBuffer

from .base import TransitionModel


def init(
    network: torch.nn.Module,
    learning_rate: float = 1e-2,
    num_iterations: int = 1000,
    batch_size: int = 64,
    # num_workers: int = 1,
    wandb_loss_name: str = "Transition model loss",
    early_stopper: EarlyStopper = None,
    device: str = "cuda",
) -> TransitionModel:
    loss_fn = torch.nn.MSELoss()
    print("trans device {}".format(device))
    print("after svgp cuda")
    if "cuda" in device:
        network.cuda()

    def predict_fn(state: State, action: Action) -> StatePrediction:
        state_action_input = torch.concat([state, action], -1)
        delta_state = network.forward(state_action_input)
        # delta_state_mean, delta_state_var, noise_var = svgp_predict_fn(
        return StatePrediction(
            state_mean=state + delta_state,
            state_var=0,
            noise_var=0,
            # state_var=delta_state_var,
            # noise_var=noise_var,
        )

    def train_fn(replay_buffer: ReplayBuffer):
        early_stopper.reset()
        network.train()
        optimizer = torch.optim.Adam(
            [{"params": network.parameters()}], lr=learning_rate
        )
        for i in range(num_iterations):
            samples = replay_buffer.sample(batch_size=batch_size)
            state_action_inputs = torch.concat(
                [samples["state"], samples["action"]], -1
            )
            state_diff = samples["next_state"] - samples["state"]

            pred = network(state_action_inputs)
            loss = loss_fn(pred, state_diff)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if wandb_loss_name is not None:
                wandb.log({wandb_loss_name: loss})

            logger.info("Iteration : {} | Loss: {}".format(i, loss))
            stop_flag = early_stopper(loss)
            if stop_flag:
                logger.info("Early stopping criteria met, stopping training")
                logger.info("Breaking out loop")
                break

    def dummy_update_fn(x: InputData, y: OutputData):
        pass

    return TransitionModel(predict=predict_fn, train=train_fn, update=dummy_update_fn)
