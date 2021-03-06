import logging
import os
import random
import string
from typing import Dict, Any, List

from .interface import AbstractExecutionEngine, AbstractGraphListener
from .. import codegen, utils
from ..graph import Model, ModelStatus, MetricData
from ..integration_api import send_trial, receive_trial_parameters, get_advisor

_logger = logging.getLogger(__name__)

class BaseGraphData:
    def __init__(self, model_script: str, training_module: str, training_kwargs: Dict[str, Any]) -> None:
        self.model_script = model_script
        self.training_module = training_module
        self.training_kwargs = training_kwargs

    def dump(self) -> dict:
        return {
            'model_script': self.model_script,
            'training_module': self.training_module,
            'training_kwargs': self.training_kwargs
        }

    @staticmethod
    def load(data):
        return BaseGraphData(data['model_script'], data['training_module'], data['training_kwargs'])


class BaseExecutionEngine(AbstractExecutionEngine):
    """
    The execution engine with no optimization at all.
    Resource management is implemented in this class.
    """

    def __init__(self) -> None:
        """
        Upon initialization, advisor callbacks need to be registered.
        Advisor will call the callbacks when the corresponding event has been triggered.
        Base execution engine will get those callbacks and broadcast them to graph listener.
        """
        self._listeners: List[AbstractGraphListener] = []

        # register advisor callbacks
        advisor = get_advisor()
        advisor.send_trial_callback = self._send_trial_callback
        advisor.request_trial_jobs_callback = self._request_trial_jobs_callback
        advisor.trial_end_callback = self._trial_end_callback
        advisor.intermediate_metric_callback = self._intermediate_metric_callback
        advisor.final_metric_callback = self._final_metric_callback

        self._running_models: Dict[int, Model] = dict()

        self.resources = 0

    def submit_models(self, *models: Model) -> None:
        for model in models:
            data = BaseGraphData(codegen.model_to_pytorch_script(model),
                                 model.training_config.module, model.training_config.kwargs)
            self._running_models[send_trial(data.dump())] = model

    def register_graph_listener(self, listener: AbstractGraphListener) -> None:
        self._listeners.append(listener)

    def _send_trial_callback(self, paramater: dict) -> None:
        if self.resources <= 0:
            _logger.warning('There is no available resource, but trial is submitted.')
        self.resources -= 1
        _logger.info('on_resource_used: %d', self.resources)

    def _request_trial_jobs_callback(self, num_trials: int) -> None:
        self.resources += num_trials
        _logger.info('on_resource_available: %d', self.resources)

    def _trial_end_callback(self, trial_id: int, success: bool) -> None:
        model = self._running_models[trial_id]
        if success:
            model.status = ModelStatus.Trained
        else:
            model.status = ModelStatus.Failed
        for listener in self._listeners:
            listener.on_training_end(model, success)

    def _intermediate_metric_callback(self, trial_id: int, metrics: MetricData) -> None:
        model = self._running_models[trial_id]
        model.intermediate_metrics.append(metrics)
        for listener in self._listeners:
            listener.on_intermediate_metric(model, metrics)

    def _final_metric_callback(self, trial_id: int, metrics: MetricData) -> None:
        model = self._running_models[trial_id]
        model.metric = metrics
        for listener in self._listeners:
            listener.on_metric(model, metrics)

    def query_available_resource(self) -> int:
        return self.resources

    @classmethod
    def trial_execute_graph(cls) -> None:
        """
        Initialize the model, hand it over to trainer.
        """
        graph_data = BaseGraphData.load(receive_trial_parameters())
        random_str = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        file_name = f'_generated_model_{random_str}.py'
        with open(file_name, 'w') as f:
            f.write(graph_data.model_script)
        trainer_cls = utils.import_(graph_data.training_module)
        model_cls = utils.import_(f'_generated_model_{random_str}._model')
        trainer_instance = trainer_cls(model=model_cls(), **graph_data.training_kwargs)
        trainer_instance.fit()
        os.remove(file_name)