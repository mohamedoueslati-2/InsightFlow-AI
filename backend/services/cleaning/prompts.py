"""Public cleaning prompts for the generative architecture."""
from .generative_prompts import SYSTEM, PLANNER, GENERATOR

CLEANING_SYSTEM_INSTRUCTION = SYSTEM
ISSUE_RESOLUTION_SYSTEM_INSTRUCTION = PLANNER
USER_START_PROMPT = 'Build an evidence-backed cleaning plan from the supplied profile.'
