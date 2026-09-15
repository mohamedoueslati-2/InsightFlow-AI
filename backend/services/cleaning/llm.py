"""Request-local Gemini client with the existing shared rotation manager."""
import asyncio
import json
from google import genai
from google.genai import errors, types
from ..profiling.agent import get_gemini_key_manager, get_gemini_model_name


class LLMUnavailable(RuntimeError):
    pass


def provider_schema(schema):
    """Send the portable schema subset; Pydantic enforces all bounds locally."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()
                    if k not in {'default', 'title', 'maxItems', 'minItems', 'maxLength', 'minLength'}}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(schema.model_json_schema())


class GeminiStructuredClient:
    def __init__(self, model=None):
        self.model = model or get_gemini_model_name()

    async def generate(self, instruction, evidence, schema):
        manager = get_gemini_key_manager()
        if not manager.api_keys:
            raise LLMUnavailable('Aucune clé Gemini configurée dans GEMINI_API_KEYS ou GEMINI_API_KEY.')
        tried = set()
        last_failure = None
        while len(tried) < len(manager.api_keys):
            key = manager.get_next_key()
            if not key or key in tried:
                break
            tried.add(key)
            # Explicit API key; never mutate process-global GOOGLE_API_KEY.
            async with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60000)).aio as client:
                try:
                    response = await asyncio.wait_for(client.models.generate_content(
                        model=self.model, contents=json.dumps(evidence, ensure_ascii=False, default=str),
                        config=types.GenerateContentConfig(system_instruction=instruction,
                            response_mime_type='application/json', response_json_schema=provider_schema(schema),
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                            temperature=0)), timeout=65)
                    result = schema.model_validate_json(response.text or '{}')
                    manager.mark_key_success(key)
                    return result
                except errors.APIError as error:
                    code = error.code
                    if code in {400, 404, 422}:
                        raise LLMUnavailable(f'Requête Gemini refusée (HTTP {code}, modèle {self.model}). '
                            'Vérifiez le modèle et le schéma de sortie ; les clés ne sont pas mises en attente.') from error
                    manager.mark_key_failed(key, error)
                    last_failure = ('Authentification Gemini refusée (HTTP 401/403).' if code in {401, 403}
                        else 'Quota Gemini atteint (HTTP 429).' if code == 429
                        else f'Service Gemini indisponible (HTTP {code}).')
                except TimeoutError as error:
                    manager.mark_key_failed(key, error)
                    last_failure = 'Délai de réponse Gemini dépassé.'
        raise LLMUnavailable(last_failure or 'Les clés Gemini sont temporairement en attente après un échec précédent. Réessayez après leur délai de reprise.')
