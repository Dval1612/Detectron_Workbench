# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import datetime
import json
import logging
import os
from collections import defaultdict
from contextlib import contextmanager
import torch
from fvcore.common.file_io import PathManager
from fvcore.common.history_buffer import HistoryBuffer

import pandas as pd

_CURRENT_STORAGE_STACK = []


def get_event_storage():
    """
    Returns:
        The :class:`EventStorage` object that's currently being used.
        Throws an error if no :class`EventStorage` is currently enabled.
    """
    assert len(
        _CURRENT_STORAGE_STACK
    ), "get_event_storage() has to be called inside a 'with EventStorage(...)' context!"
    return _CURRENT_STORAGE_STACK[-1]


class EventWriter:
    """
    Base class for writers that obtain events from :class:`EventStorage` and process them.
    """

    def write(self):
        raise NotImplementedError

    def close(self):
        pass


class PDWriter(EventWriter):
    def __init__(self, pd_frame, cfg, log_frequency, log_path=""):
        self.logger = logging.getLogger(__name__)
        self.pd_frame = pd_frame
        self.cfg = cfg
        self.log_frequency = log_frequency
        self.log_path = log_path

    def write(self):
        storage = get_event_storage()
        iteration = storage.iter
        lr = storage.history("lr").latest()

        losses = {}
        for k, v in storage.histories().items():
            if "loss" in k:
                losses[k] = v.median(1)

        time = 0.0
        data_time = 0.0
        try:
            time = storage.history("time").avg(1)
            data_time = storage.history("data_time").avg(1)
        except KeyError:  # they may not exist in the first few iterations (due to warmup)
            pass

        df = pd.DataFrame(
            [
                {"iter": iteration, "legend": "elapsed_time", "value": time},
                {"iter": iteration, "legend": "loss_rpn_box_reg", "value": losses["loss_rpn_loc"]},
                {"iter": iteration, "legend": "loss_objectness", "value": losses["loss_rpn_cls"]},
                {"iter": iteration, "legend": "loss_box_reg", "value": losses["loss_box_reg"]},
                {"iter": iteration, "legend": "loss_classifier", "value": losses["loss_cls"]},
                {"iter": iteration, "legend": "loss_mask", "value": losses["loss_mask"]},
                {"iter": iteration, "legend": "lr", "value": lr},
                {
                    "iter": iteration,
                    "legend": "max_mem",
                    "value": torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                },
                {"iter": iteration, "legend": "loader_time", "value": data_time},
            ]
        )
        self.pd_frame = pd.concat([self.pd_frame, df], axis=0, sort=False)

        if iteration % self.log_frequency == 0:
            npy_file_name = "torch-d2-{}-batch_size-{}-image_dir-{}-{}.csv".format(
                iteration,
                self.cfg.SOLVER.IMS_PER_BATCH,
                self.cfg.DATASETS.TRAIN[0],
                str(datetime.datetime.now().strftime("%Y-%m-%d--%H-%M-%S")),
            )
            log_dir = os.path.join(
                self.log_path, "csv".format(self.cfg.SOLVER.IMS_PER_BATCH)
            )
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            npy_file_name = os.path.join(log_dir, npy_file_name)
            self.pd_frame.to_csv(npy_file_name, index=False)
            print("saved: {}".format(npy_file_name))


class JSONWriter(EventWriter):
    """
    Write scalars to a json file.

    It saves scalars as one json per line (instead of a big json) for easy parsing.

    Examples parsing such a json file:

    .. code-block:: none

        $ cat metrics.json | jq -s '.[0:2]'
        [
          {
            "data_time": 0.008433341979980469,
            "iteration": 20,
            "loss": 1.9228371381759644,
            "loss_box_reg": 0.050025828182697296,
            "loss_classifier": 0.5316952466964722,
            "loss_mask": 0.7236229181289673,
            "loss_rpn_box": 0.0856662318110466,
            "loss_rpn_cls": 0.48198649287223816,
            "lr": 0.007173333333333333,
            "time": 0.25401854515075684
          },
          {
            "data_time": 0.007216215133666992,
            "iteration": 40,
            "loss": 1.282649278640747,
            "loss_box_reg": 0.06222952902317047,
            "loss_classifier": 0.30682939291000366,
            "loss_mask": 0.6970193982124329,
            "loss_rpn_box": 0.038663312792778015,
            "loss_rpn_cls": 0.1471673548221588,
            "lr": 0.007706666666666667,
            "time": 0.2490077018737793
          }
        ]

        $ cat metrics.json | jq '.loss_mask'
        0.7126231789588928
        0.689423680305481
        0.6776131987571716
        ...

    """

    def __init__(self, json_file, window_size=1):
        """
        Args:
            json_file (str): path to the json file. New data will be appended if the file exists.
            window_size (int): the window size of median smoothing for the scalars whose
                `smoothing_hint` are True.
        """
        self._file_handle = PathManager.open(json_file, "a")
        self._window_size = window_size

    def write(self):
        storage = get_event_storage()
        to_save = {"iteration": storage.iter}
        to_save.update(storage.latest_with_smoothing_hint(self._window_size))
        self._file_handle.write(json.dumps(to_save, sort_keys=True) + "\n")
        self._file_handle.flush()
        try:
            os.fsync(self._file_handle.fileno())
        except AttributeError:
            pass

    def close(self):
        self._file_handle.close()


class TensorboardXWriter(EventWriter):
    """
    Write all scalars to a tensorboard file.
    """

    def __init__(self, log_dir: str, window_size: int = 1, **kwargs):
        """
        Args:
            log_dir (str): the directory to save the output events
            window_size (int): the scalars will be median-smoothed by this window size

            kwargs: other arguments passed to `torch.utils.tensorboard.SummaryWriter(...)`
        """
        self._window_size = window_size
        from torch.utils.tensorboard import SummaryWriter

        self._writer = SummaryWriter(log_dir, **kwargs)

    def write(self):
        storage = get_event_storage()
        for k, v in storage.latest_with_smoothing_hint(self._window_size).items():
            self._writer.add_scalar(k, v, storage.iter)

        if len(storage.vis_data) >= 1:
            for img_name, img, step_num in storage.vis_data:
                self._writer.add_image(img_name, img, step_num)
            storage.clear_images()

    def close(self):
        if hasattr(self, "_writer"):  # doesn't exist when the code fails at import
            self._writer.close()


class CommonMetricPrinter(EventWriter):
    """
    Print **common** metrics to the terminal, including
    iteration time, ETA, memory, all losses, and the learning rate.

    To print something different, please implement a similar printer by yourself.
    """

    def __init__(self, max_iter):
        """
        Args:
            max_iter (int): the maximum number of iterations to train.
                Used to compute ETA.
        """
        self.logger = logging.getLogger(__name__)
        self._max_iter = max_iter

    def write(self):
        storage = get_event_storage()
        iteration = storage.iter

        data_time, time = None, None
        eta_string = "N/A"
        try:
            data_time = storage.history("data_time").avg(1)
            time = storage.history("time").global_avg()
            eta_seconds = storage.history("time").median(1000) * (self._max_iter - iteration)
            storage.put_scalar("eta_seconds", eta_seconds, smoothing_hint=False)
            eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))
        except KeyError:  # they may not exist in the first few iterations (due to warmup)
            pass

        try:
            lr = "{:.6f}".format(storage.history("lr").latest())
        except KeyError:
            lr = "N/A"

        if torch.cuda.is_available():
            max_mem_mb = torch.cuda.max_memory_allocated() / 1024.0 / 1024.0
        else:
            max_mem_mb = None

        # NOTE: max_mem is parsed by grep in "dev/parse_results.sh"
        self.logger.info(
            """\
eta: {eta}  iter: {iter}  {losses}  \
{time}  {data_time}  \
lr: {lr}  {memory}\
""".format(
                eta=eta_string,
                iter=iteration,
                losses="  ".join(
                    [
                        "{}: {:.3f}".format(k, v.median(1))
                        for k, v in storage.histories().items()
                        if "loss" in k
                    ]
                ),
                time="time: {:.4f}".format(time) if time is not None else "",
                data_time="data_time: {:.4f}".format(data_time) if data_time is not None else "",
                lr=lr,
                memory="max_mem: {:.0f}M".format(max_mem_mb) if max_mem_mb is not None else "",
            )
        )


class EventStorage:
    """
    The user-facing class that provides metric storage functionalities.

    In the future we may add support for storing / logging other types of data if needed.
    """

    def __init__(self, start_iter=0):
        """
        Args:
            start_iter (int): the iteration number to start with
        """
        self._history = defaultdict(HistoryBuffer)
        self._smoothing_hints = {}
        self._latest_scalars = {}
        self._iter = start_iter
        self._current_prefix = ""
        self._vis_data = []

    def put_image(self, img_name, img_tensor):
        """
        Add an `img_tensor` to the `_vis_data` associated with `img_name`.

        Args:
            img_name (str): The name of the image to put into tensorboard.
            img_tensor (torch.Tensor or numpy.array): An `uint8` or `float`
                Tensor of shape `[channel, height, width]` where `channel` is
                3. The image format should be RGB. The elements in img_tensor
                can either have values in [0, 1] (float32) or [0, 255] (uint8).
                The `img_tensor` will be visualized in tensorboard.
        """
        self._vis_data.append((img_name, img_tensor, self._iter))

    def clear_images(self):
        """
        Delete all the stored images for visualization. This should be called
        after images are written to tensorboard.
        """
        self._vis_data = []

    def put_scalar(self, name, value, smoothing_hint=True):
        """
        Add a scalar `value` to the `HistoryBuffer` associated with `name`.

        Args:
            smoothing_hint (bool): a 'hint' on whether this scalar is noisy and should be
                smoothed when logged. The hint will be accessible through
                :meth:`EventStorage.smoothing_hints`.  A writer may ignore the hint
                and apply custom smoothing rule.

                It defaults to True because most scalars we save need to be smoothed to
                provide any useful signal.
        """
        name = self._current_prefix + name
        history = self._history[name]
        value = float(value)
        history.update(value, self._iter)
        self._latest_scalars[name] = value

        existing_hint = self._smoothing_hints.get(name)
        if existing_hint is not None:
            assert (
                existing_hint == smoothing_hint
            ), "Scalar {} was put with a different smoothing_hint!".format(name)
        else:
            self._smoothing_hints[name] = smoothing_hint

    def put_scalars(self, *, smoothing_hint=True, **kwargs):
        """
        Put multiple scalars from keyword arguments.

        Examples:

            storage.put_scalars(loss=my_loss, accuracy=my_accuracy, smoothing_hint=True)
        """
        for k, v in kwargs.items():
            self.put_scalar(k, v, smoothing_hint=smoothing_hint)

    def history(self, name):
        """
        Returns:
            HistoryBuffer: the scalar history for name
        """
        ret = self._history.get(name, None)
        if ret is None:
            raise KeyError("No history metric available for {}!".format(name))
        return ret

    def histories(self):
        """
        Returns:
            dict[name -> HistoryBuffer]: the HistoryBuffer for all scalars
        """
        return self._history

    def latest(self):
        """
        Returns:
            dict[name -> number]: the scalars that's added in the current iteration.
        """
        return self._latest_scalars

    def latest_with_smoothing_hint(self, window_size=1):
        """
        Similar to :meth:`latest`, but the returned values
        are either the un-smoothed original latest value,
        or a median of the given window_size,
        depend on whether the smoothing_hint is True.

        This provides a default behavior that other writers can use.
        """
        result = {}
        for k, v in self._latest_scalars.items():
            result[k] = self._history[k].median(window_size) if self._smoothing_hints[k] else v
        return result

    def smoothing_hints(self):
        """
        Returns:
            dict[name -> bool]: the user-provided hint on whether the scalar
                is noisy and needs smoothing.
        """
        return self._smoothing_hints

    def step(self):
        """
        User should call this function at the beginning of each iteration, to
        notify the storage of the start of a new iteration.
        The storage will then be able to associate the new data with the
        correct iteration number.
        """
        self._iter += 1
        self._latest_scalars = {}

    @property
    def vis_data(self):
        return self._vis_data

    @property
    def iter(self):
        return self._iter

    @property
    def iteration(self):
        # for backward compatibility
        return self._iter

    def __enter__(self):
        _CURRENT_STORAGE_STACK.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        assert _CURRENT_STORAGE_STACK[-1] == self
        _CURRENT_STORAGE_STACK.pop()

    @contextmanager
    def name_scope(self, name):
        """
        Yields:
            A context within which all the events added to this storage
            will be prefixed by the name scope.
        """
        old_prefix = self._current_prefix
        self._current_prefix = name.rstrip("/") + "/"
        yield
        self._current_prefix = old_prefix
