import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from ..schemas.presentation import (
    GeneratePresentationPlanRequest,
    PresentationChatRequest,
    PresentationConversation,
    PresentationConversationMessage,
    PresentationInteractiveTurn,
    PresentationAgentTurn,
    PresentationContext,
    PresentationPlan,
    PresentationSetupSuggestion,
)
from ..services.job_service import job_directory
from ..services.presentation_agent.agent import PresentationPlanningAgent, runtime_status
from ..services.presentation_agent.plan_service import load_plan, save_plan
from ..services.presentation_agent.conversation_service import (
    load_conversation,
    reset_conversation,
    save_conversation,
)
from ..services.presentation_agent.prompt_builder import build_presenton_prompt, save_presenton_prompt
from ..services.presentation_catalog import PresentationCatalog
from ..services.presentation_package import PresentationPackageService

router = APIRouter(prefix='/api/jobs/{job_id}/presentation', tags=['presentation'])
logger = logging.getLogger(__name__)


def directory(job_id: str):
    result = job_directory(job_id)
    if result is None:
        raise HTTPException(404, 'Job not found.')
    return result


@router.get('/context', response_model=PresentationContext)
def context(job_id: str):
    job_dir = directory(job_id)
    catalog = PresentationCatalog(job_dir)
    status = runtime_status()
    return PresentationContext(
        jobId=job_id,
        title=catalog.title,
        sectionCount=len(catalog.list_sections()),
        tableCount=len(catalog.list_tables()),
        assetCount=len(catalog.list_assets()),
        sections=catalog.list_sections(),
        tables=catalog.list_tables(),
        assets=catalog.list_assets(),
        closingGuidance=catalog.get_closing_guidance(),
        agentConfigured=bool(status['configured'] and status['adkAvailable']),
        model=status['model'],
    )


@router.post('/suggest-setup', response_model=PresentationSetupSuggestion)
async def suggest_setup(job_id: str):
    job_dir = directory(job_id)
    catalog = PresentationCatalog(job_dir)
    agent = PresentationPlanningAgent(catalog)
    try:
        return await agent.suggest_setup()
    except RuntimeError as exc:
        message = str(exc)
        status = 503 if ('not configured' in message.lower() or 'dependencies' in message.lower() or 'unavailable' in message.lower()) else 502
        raise HTTPException(status, message) from exc
    except Exception:
        logger.exception('Presentation setup suggestion failed')
        raise HTTPException(502, 'Presentation Agent failed to suggest presentation settings.')


@router.post('/plan', response_model=PresentationAgentTurn)
async def generate_plan(job_id: str, request: GeneratePresentationPlanRequest):
    job_dir = directory(job_id)
    catalog = PresentationCatalog(job_dir)
    current = load_plan(job_dir)
    agent = PresentationPlanningAgent(catalog)
    try:
        turn = await agent.generate(request.preferences, current_plan=current)
        save_plan(job_dir, turn.plan)
        save_presenton_prompt(job_dir, turn.plan)
        reset_conversation(job_dir, turn.assistantMessage)
        return turn
    except RuntimeError as exc:
        message = str(exc)
        status = 503 if ('not configured' in message.lower() or 'dependencies' in message.lower() or 'unavailable' in message.lower()) else 502
        raise HTTPException(status, message) from exc
    except Exception:
        logger.exception('Presentation plan generation failed')
        raise HTTPException(502, 'Presentation Agent failed to create a valid plan.')


@router.get('/plan', response_model=PresentationPlan)
def get_plan(job_id: str):
    plan = load_plan(directory(job_id))
    if plan is None:
        raise HTTPException(404, 'Presentation plan not found.')
    return plan


@router.put('/plan', response_model=PresentationPlan)
def update_plan(job_id: str, plan: PresentationPlan):
    job_dir = directory(job_id)
    if plan.jobId != job_id:
        raise HTTPException(400, 'Plan jobId does not match URL job_id.')
    save_plan(job_dir, plan)
    save_presenton_prompt(job_dir, plan)
    return plan


@router.get('/presenton-prompt', response_class=PlainTextResponse)
def presenton_prompt(job_id: str):
    job_dir = directory(job_id)
    plan = load_plan(job_dir)
    if plan is None:
        raise HTTPException(404, 'Presentation plan not found.')
    return PlainTextResponse(build_presenton_prompt(plan), media_type='text/plain')


@router.get('/conversation', response_model=PresentationConversation)
def get_conversation(job_id: str):
    return load_conversation(directory(job_id))


@router.delete('/conversation', response_model=PresentationConversation)
def clear_conversation(job_id: str):
    job_dir = directory(job_id)
    plan = load_plan(job_dir)
    initial = 'The presentation plan is ready. Tell me what you want to change or ask about the current story.' if plan else None
    return reset_conversation(job_dir, initial)


@router.post('/chat', response_model=PresentationInteractiveTurn)
async def interactive_chat(job_id: str, request: PresentationChatRequest):
    job_dir = directory(job_id)
    current = load_plan(job_dir)
    if current is None:
        raise HTTPException(409, 'Generate a presentation plan before starting the interactive session.')

    catalog = PresentationCatalog(job_dir)
    agent = PresentationPlanningAgent(catalog)
    conversation = load_conversation(job_dir)
    try:
        turn = await agent.chat(request.message, current, conversation.messages)
        save_plan(job_dir, turn.plan)
        save_presenton_prompt(job_dir, turn.plan)
        conversation.messages.extend([
            PresentationConversationMessage(role='user', content=request.message),
            PresentationConversationMessage(role='assistant', content=turn.assistantMessage),
        ])
        save_conversation(job_dir, conversation)
        return turn
    except RuntimeError as exc:
        message = str(exc)
        status = 503 if ('not configured' in message.lower() or 'dependencies' in message.lower() or 'unavailable' in message.lower()) else 502
        raise HTTPException(status, message) from exc
    except Exception:
        logger.exception('Interactive presentation refinement failed')
        raise HTTPException(502, 'Presentation Agent failed to refine the current plan.')


@router.get('/download-package')
def download_package(job_id: str):
    job_dir = directory(job_id)
    plan = load_plan(job_dir)
    if plan is None:
        raise HTTPException(409, 'Generate a presentation plan before downloading the handoff package.')
    try:
        package_path = PresentationPackageService().build(job_dir, plan)
        return FileResponse(
            package_path,
            media_type='application/zip',
            filename='presentation-handoff.zip',
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        logger.exception('Presentation package generation failed')
        raise HTTPException(500, 'Could not prepare the presentation download package.')
