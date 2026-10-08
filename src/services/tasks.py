from typing import Dict, Optional, Type

from src.core.config import Settings
from src.services.task_ai_info import AiInfoTask
from src.services.task_base import BaseTask
from src.services.task_event import EventTask
from src.services.task_extraction import ExtractionTask

TASKS: Dict[str, Type[BaseTask]] = {
    "event": EventTask,
    "ai_info": AiInfoTask,
    "extraction": ExtractionTask,
}


def get_task(function: str, settings: Optional[Settings] = None) -> BaseTask:
    return TASKS[function](settings)
