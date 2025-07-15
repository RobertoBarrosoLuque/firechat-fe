import asyncio
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import json
import os
import uuid
from src.modules.llm_completion import FireworksStreamer, FireworksConfig
from src.modules.benchmark import FireworksBenchmarkService
from src.modules.session import SessionManager
from src.logger import logger


# Initialize FastAPI app
app = FastAPI(
    title="Fireworks Chat & Benchmark API",
    description="API for chat interactions and performance benchmarking with Fireworks models",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
config = FireworksConfig()
api_key = os.getenv("FIREWORKS_API_KEY")
if not api_key:
    raise ValueError("FIREWORKS_API_KEY environment variable is required")

streamer = FireworksStreamer(api_key)
benchmark_service = FireworksBenchmarkService(api_key)
session_manager = SessionManager(config.config)


# Pydantic models
class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: 'user' or 'assistant'")
    content: str = Field(..., description="Message content")


class SingleChatRequest(BaseModel):
    model_key: str = Field(..., description="Model key from config")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    temperature: Optional[float] = Field(0.7, description="Sampling temperature")
    conversation_id: Optional[str] = Field(
        None, description="Conversation ID for tracking"
    )


class ChatCompletionRequest(BaseModel):
    model_key: str = Field(..., description="Model key from config")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    temperature: Optional[float] = Field(0.7, description="Sampling temperature")
    conversation_id: Optional[str] = Field(
        None, description="Conversation ID for tracking"
    )


class ComparisonChatRequest(BaseModel):
    model_keys: List[str] = Field(..., description="Two model keys to compare")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    temperature: Optional[float] = Field(0.7, description="Sampling temperature")
    comparison_id: Optional[str] = Field(None, description="Comparison ID for tracking")
    speed_test: bool = Field(False, description="Enable speed test benchmarking")
    concurrency: int = Field(
        1, ge=1, le=50, description="Number of concurrent requests for speed test"
    )


class BenchmarkConfigRequest(BaseModel):
    model_key: str = Field(..., description="Model key to benchmark")
    prompt: str = Field(..., description="Test prompt")
    concurrency: int = Field(
        10, ge=1, le=100, description="Number of concurrent requests"
    )
    max_tokens: int = Field(
        256, ge=50, le=1024, description="Maximum tokens per request"
    )
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="Sampling temperature")


class ComparisonBenchmarkRequest(BaseModel):
    model_keys: List[str] = Field(..., description="Model keys to compare")
    prompt: str = Field(..., description="Test prompt")
    concurrency: int = Field(
        10, ge=1, le=100, description="Number of concurrent requests"
    )
    max_tokens: int = Field(
        256, ge=50, le=1024, description="Maximum tokens per request"
    )
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="Sampling temperature")


def validate_model_key(model_key: str) -> bool:
    """Validate that model key exists in config"""
    try:
        config.get_model(model_key)
    except ValueError:
        return False
    return True


def generate_session_id() -> str:
    """Generate unique session ID"""
    return str(uuid.uuid4())


async def _stream_response(
    model_key: str,
    messages: List[Dict[str, Any]],
    session_id: str,
    temperature: Optional[float],
    error_context: str,
):
    """Helper to stream chat responses."""
    try:
        async for chunk in streamer.stream_chat_completion(
            model_key=model_key,
            messages=messages,
            request_id=session_id,
            temperature=temperature,
        ):
            # Format as server-sent events
            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"

        # Send completion signal
        yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"

    except Exception as e:
        logger.error(f"Error in {error_context}: {str(e)}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


async def _stream_response_with_session(
    model_key: str,
    messages: List[Dict[str, Any]],
    session_id: str,
    temperature: Optional[float],
    error_context: str,
):
    """Helper to stream chat responses and save assistant responses to session."""
    assistant_content = ""

    try:
        async for chunk in streamer.stream_chat_completion(
            model_key=model_key,
            messages=messages,
            request_id=session_id,
            temperature=temperature,
        ):
            assistant_content += chunk
            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"

        # Save assistant response to session
        if assistant_content:
            session_manager.add_assistant_message(
                session_id, assistant_content, model_key
            )

        # Send completion signal
        yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"

    except Exception as e:
        logger.error(f"Error in {error_context}: {str(e)}")
        yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"


@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Fireworks Chat & Benchmark API", "status": "healthy"}


@app.get("/models")
async def get_available_models():
    """Get all available models"""
    try:
        models = config.get_all_models()
        return {"models": models}
    except Exception as e:
        logger.error(f"Error getting models: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get available models")


@app.get("/models/{model_key}")
async def get_model_info(model_key: str):
    """Get detailed information about a specific model"""
    try:
        if not validate_model_key(model_key):
            raise HTTPException(
                status_code=404, detail=f"Model '{model_key}' not found"
            )

        model_info = config.get_model(model_key)
        return {"model": model_info}
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Model '{model_key}' not found")
    except Exception as e:
        logger.error(f"Error getting model info: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get model information")


@app.get("/sessions/stats")
async def get_session_stats():
    """Get session management statistics"""
    try:
        stats = session_manager.get_session_stats()
        return {"session_stats": stats}
    except Exception as e:
        logger.error(f"Error getting session stats: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get session statistics")


@app.get("/sessions")
async def list_sessions():
    """List all active sessions"""
    try:
        sessions = session_manager.list_sessions()
        return {"sessions": sessions}
    except Exception as e:
        logger.error(f"Error listing sessions: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list sessions")


@app.post("/chat/single")
async def single_chat(request: SingleChatRequest):
    """Single model chat with streaming response and conversation history"""
    try:
        if not validate_model_key(request.model_key):
            raise HTTPException(
                status_code=400, detail=f"Invalid model key: {request.model_key}"
            )

        session_id = request.conversation_id or generate_session_id()

        logger.info(f"Chat request for session: {session_id}")

        # Get or create session
        session_manager.get_or_create_session(
            session_id=session_id, model_key=request.model_key, session_type="single"
        )

        if request.messages:
            latest_message = request.messages[-1]  # Get the last message
            if latest_message.role == "user":
                session_manager.add_user_message(session_id, latest_message.content)

        messages_dict = session_manager.get_conversation_history(session_id)

        logger.info(f"Chat history for session: {session_id} \n {messages_dict}")

        return StreamingResponse(
            _stream_response_with_session(
                model_key=request.model_key,
                messages=messages_dict,
                session_id=session_id,
                temperature=request.temperature,
                error_context="single chat",
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Session-ID": session_id,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in single chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Chat request failed")


@app.post("/chat/compare")
async def comparison_chat(request: ComparisonChatRequest):
    """Side-by-side model comparison chat with conversation history"""
    try:
        if len(request.model_keys) != 2:
            raise HTTPException(
                status_code=400, detail="Exactly 2 model keys required for comparison"
            )

        for model_key in request.model_keys:
            if not validate_model_key(model_key):
                raise HTTPException(
                    status_code=400, detail=f"Invalid model key: {model_key}"
                )
        comparison_id = request.comparison_id or generate_session_id()
        # Get or create session for comparison
        session_manager.get_or_create_session(
            session_id=comparison_id,
            model_keys=request.model_keys,
            session_type="compare",
        )

        if request.messages:
            latest_message = request.messages[-1]
            if latest_message.role == "user":
                session_manager.add_user_message(comparison_id, latest_message.content)

        messages_dict = session_manager.get_conversation_history(comparison_id)

        async def generate_comparison():
            try:
                messages = messages_dict
                assistant_contents = ["", ""]  # Track responses for both models

                # Create async generators for both models with performance metrics enabled
                generators = {}
                for i, model_key in enumerate(request.model_keys):
                    generators[f"model_{i}"] = streamer.stream_chat_completion(
                        model_key=model_key,
                        messages=messages,
                        request_id=f"{comparison_id}_{i}",
                        temperature=request.temperature,
                        enable_perf_metrics=True,
                    )

                # Stream responses from both models
                active_generators = set(generators.keys())

                while active_generators:
                    for gen_key in list(active_generators):
                        try:
                            chunk = await generators[gen_key].__anext__()
                            model_index = int(gen_key.split("_")[1])
                            model_key = request.model_keys[model_index]

                            # Track content for session saving
                            assistant_contents[model_index] += chunk

                            yield f"""data: {json.dumps({
                                'type': 'content',
                                'model_index': model_index,
                                'model_key': model_key,
                                'content': chunk
                            })}\n\n"""

                        except StopAsyncIteration:
                            active_generators.remove(gen_key)
                            model_index = int(gen_key.split("_")[1])

                            yield f"""data: {json.dumps({
                                'type': 'model_done',
                                'model_index': model_index,
                                'model_key': request.model_keys[model_index]
                            })}\n\n"""

                        except Exception as e:
                            logger.error(f"Error in model {gen_key}: {str(e)}")
                            active_generators.remove(gen_key)
                            model_index = int(gen_key.split("_")[1])

                            yield f"""data: {json.dumps({
                                'type': 'error',
                                'model_index': model_index,
                                'model_key': request.model_keys[model_index],
                                'error': str(e)
                            })}\n\n"""

                    await asyncio.sleep(0.001)

                for i, content in enumerate(assistant_contents):
                    if content:
                        session_manager.add_assistant_message(
                            comparison_id, content, request.model_keys[i]
                        )

                if request.speed_test:
                    try:
                        logger.info(
                            f"Starting live speed test for models: {request.model_keys} "
                            f"with concurrency: {request.concurrency}"
                        )

                        user_messages = [
                            msg for msg in messages if msg.get("role") == "user"
                        ]
                        last_user_message = (
                            user_messages[-1]["content"]
                            if user_messages
                            else "Hello, world!"
                        )

                        live_metrics_data = []

                        async def live_metrics_callback(metrics: Dict[str, Any]):
                            live_metrics_data.append(metrics)

                        # Start the benchmark in the background
                        benchmark_task = asyncio.create_task(
                            benchmark_service.run_live_comparison_benchmark(
                                model_keys=request.model_keys,
                                prompt=last_user_message,
                                concurrency=request.concurrency,
                                max_tokens=100,  # Shorter tokens for speed test
                                temperature=request.temperature,
                                live_metrics_callback=live_metrics_callback,
                            )
                        )

                        last_metrics_count = 0
                        while not benchmark_task.done():
                            # Check if we have new metrics to stream
                            if len(live_metrics_data) > last_metrics_count:
                                for i in range(
                                    last_metrics_count, len(live_metrics_data)
                                ):
                                    yield f"""data: {json.dumps({
                                        'type': 'live_metrics',
                                        'metrics': live_metrics_data[i]
                                    })}\n\n"""
                                last_metrics_count = len(live_metrics_data)

                            # Small delay to prevent tight loop
                            await asyncio.sleep(0.001)

                        # Stream any final metrics
                        if len(live_metrics_data) > last_metrics_count:
                            for i in range(last_metrics_count, len(live_metrics_data)):
                                yield f"""data: {json.dumps({
                                    'type': 'live_metrics',
                                    'metrics': live_metrics_data[i]
                                })}\n\n"""

                        # Wait for benchmark completion and get final results
                        benchmark_results = await benchmark_task

                        if benchmark_results:
                            # Extract speed test metrics
                            model1_result = benchmark_results[request.model_keys[0]]
                            model2_result = benchmark_results[request.model_keys[1]]

                            speed_test_data = {
                                "model1_tps": model1_result.avg_tokens_per_second,
                                "model2_tps": model2_result.avg_tokens_per_second,
                                "model1_rps": model1_result.requests_per_second,
                                "model2_rps": model2_result.requests_per_second,
                                "model1_ttft": model1_result.avg_time_to_first_token
                                * 1000,  # Convert to ms
                                "model2_ttft": model2_result.avg_time_to_first_token
                                * 1000,
                                "model1_times": [
                                    r.get("total_time", 0) * 1000
                                    for r in model1_result.individual_results
                                    if "total_time" in r
                                ],
                                "model2_times": [
                                    r.get("total_time", 0) * 1000
                                    for r in model2_result.individual_results
                                    if "total_time" in r
                                ],
                                "concurrency": request.concurrency,
                                "model1_aggregate_tps": model1_result.aggregate_tokens_per_second,
                                "model2_aggregate_tps": model2_result.aggregate_tokens_per_second,
                                "model1_completed_requests": model1_result.successful_requests,
                                "model2_completed_requests": model2_result.successful_requests,
                                "total_requests": model1_result.total_requests,
                            }

                            # Stream final speed test results
                            yield f"""data: {json.dumps({
                                'type': 'speed_test_results',
                                'results': speed_test_data
                            })}\n\n"""

                            logger.info(
                                f"Speed test completed: Model1 TPS={speed_test_data['model1_tps']:.2f}, Model2 TPS={speed_test_data['model2_tps']:.2f}"
                            )

                    except Exception as e:
                        logger.error(f"Error in speed test: {str(e)}")
                        yield f"""data: {json.dumps({
                            'type': 'speed_test_error',
                            'error': str(e)
                        })}\n\n"""

                # Send final completion signal
                yield f"data: {json.dumps({'type': 'comparison_done','comparison_id': comparison_id})}\n\n"

            except Exception as e:
                logger.error(f"Error in comparison chat: {str(e)}")
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        return StreamingResponse(
            generate_comparison(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Comparison-ID": comparison_id,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in comparison chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="Comparison chat failed")


# Error handlers
@app.exception_handler(404)
async def not_found_handler(request, exc):
    return JSONResponse(
        status_code=404, content={"error": "Endpoint not found", "detail": str(exc)}
    )


@app.exception_handler(500)
async def internal_error_handler(request, exc):
    logger.error(f"Internal server error: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An unexpected error occurred",
        },
    )


# Background session cleanup task
async def cleanup_expired_sessions():
    """Background task to clean up expired sessions"""
    while True:
        try:
            cleanup_interval = config.config.get("chat", {}).get(
                "cleanup_interval_minutes", 60
            )
            session_timeout = config.config.get("chat", {}).get(
                "session_timeout_hours", 1
            )

            # Clean up expired sessions
            removed_count = session_manager.cleanup_expired_sessions(session_timeout)

            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} expired sessions")

            # Wait for the next cleanup interval
            await asyncio.sleep(cleanup_interval * 60)

        except Exception as e:
            logger.error(f"Error in session cleanup task: {str(e)}")
            # Wait 5 minutes before retrying on error
            await asyncio.sleep(300)


# Startup event
@app.on_event("startup")
async def startup_event():
    logger.info("Fireworks Chat & Benchmark API starting up...")
    logger.info(f"Available models: {list(config.get_all_models().keys())}")

    # Start the background session cleanup task
    asyncio.create_task(cleanup_expired_sessions())
    logger.info("Session cleanup task started")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_routes:app", host="0.0.0.0", port=8000, reload=True, log_level="info"
    )
