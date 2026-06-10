import httpx
import asyncio
import time
from src.core.config import config
from src.core.logger import logger

class OpenRouterClient:
    def __init__(self):
        self.api_key = config.openrouter_api_key
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not configured in .env")
            
    async def request_completion(self, model: str, messages: list, temperature: float = 0.2, max_retries: int = 5) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/manga-recap-orchestrator",
            "X-Title": "Manga Recap Orchestrator"
        }
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature
        }
        
        backoff = 2.0
        
        for attempt in range(max_retries):
            try:
                # Generous timeouts for large prompt generation and reasoning models
                timeout_config = httpx.Timeout(120.0, connect=15.0, read=90.0, write=15.0)
                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    response = await asyncio.wait_for(
                        client.post(self.base_url, headers=headers, json=payload),
                        timeout=125.0
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        choices = data.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content")
                            if content is None:
                                logger.error(f"OpenRouter message content is None: {data}")
                                raise ValueError("Empty response content from OpenRouter")
                            return content.strip()
                        else:
                            logger.error(f"OpenRouter empty choices: {data}")
                            raise ValueError("Empty response from OpenRouter")
                            
                    elif response.status_code == 429:
                        logger.warning(f"Rate limited (429) on attempt {attempt + 1}. Retrying in {backoff}s...")
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        
                    else:
                        logger.error(f"OpenRouter HTTP Error {response.status_code}: {response.text}")
                        # If it's a 5xx error, retry. Otherwise fail immediately.
                        if response.status_code >= 500:
                            await asyncio.sleep(backoff)
                            backoff *= 2.0
                        else:
                            raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
                            
            except (httpx.RequestError, asyncio.TimeoutError, ValueError) as e:
                logger.warning(f"Request failed: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff *= 2.0
                
        raise RuntimeError(f"OpenRouter API call failed after {max_retries} attempts.")

openrouter_client = OpenRouterClient()
