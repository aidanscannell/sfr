#!/usr/bin/env python3
import logging
from typing import Callable, NamedTuple


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from src.custom_types import Action, StatePrediction, State, Data
from torch.utils.data import DataLoader


class TransitionModel(NamedTuple):
    predict: Callable[[State, Action, Data], StatePrediction]
    train: Callable[[DataLoader], dict]
