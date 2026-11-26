from __future__ import annotations

from typing import Any

_DEFAULT_API_VERSION = "0.4.0"


def build_openapi_spec(version: str = _DEFAULT_API_VERSION) -> dict[str, Any]:
    """Build the OpenAPI 3.1.0 specification dictionary for the router HTTP server."""
    bearer_security: list[dict[str, list[str]]] = [{"BearerAuth": []}]
    error_response = {
        "description": "Error response",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
            }
        },
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Cost-Aware Model Router API",
            "version": version,
            "description": (
                "OpenAI-compatible cost-aware LLM routing gateway with Pareto policy "
                "selection, FinOps economics, circuit breakers, and tenant governance."
            ),
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            },
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["message"],
                        }
                    },
                    "required": ["error"],
                },
                "ModelObject": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "object": {"type": "string", "enum": ["model"]},
                        "owned_by": {"type": "string"},
                        "tier": {"type": "string", "enum": ["compact", "mid", "frontier"]},
                    },
                    "required": ["id", "object", "owned_by", "tier"],
                },
            },
        },
        "paths": {
            "/healthz": {
                "get": {
                    "summary": "Liveness probe",
                    "responses": {
                        "200": {
                            "description": "Server is alive",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/readyz": {
                "get": {
                    "summary": "Readiness probe",
                    "responses": {
                        "200": {
                            "description": "Server is ready to serve traffic",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "503": {
                            "description": "Server is not ready",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "OpenAPI 3.1.0 specification",
                    "responses": {
                        "200": {
                            "description": "OpenAPI specification document",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/v1/models": {
                "get": {
                    "summary": "List available catalog models",
                    "security": bearer_security,
                    "responses": {
                        "200": {
                            "description": "List of available models",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "object": {"type": "string"},
                                            "data": {
                                                "type": "array",
                                                "items": {
                                                    "$ref": "#/components/schemas/ModelObject"
                                                },
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "401": error_response,
                    },
                }
            },
            "/v1/models/{model_id}": {
                "get": {
                    "summary": "Retrieve a single catalog model",
                    "security": bearer_security,
                    "parameters": [
                        {
                            "name": "model_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Catalog model details",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ModelObject"}
                                }
                            },
                        },
                        "401": error_response,
                        "404": error_response,
                    },
                }
            },
            "/v1/chat/completions": {
                "post": {
                    "summary": "Create a routed chat completion",
                    "security": bearer_security,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "model": {"type": "string"},
                                        "messages": {
                                            "type": "array",
                                            "items": {"type": "object"},
                                        },
                                        "stream": {"type": "boolean"},
                                    },
                                    "required": ["messages"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Chat completion response or SSE stream",
                            "content": {
                                "application/json": {"schema": {"type": "object"}},
                                "text/event-stream": {"schema": {"type": "string"}},
                            },
                        },
                        "400": error_response,
                        "401": error_response,
                        "429": error_response,
                    },
                }
            },
            "/v1/finops/economics": {
                "get": {
                    "summary": "Summarize FinOps economics and savings",
                    "security": bearer_security,
                    "parameters": [
                        {
                            "name": "org_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "FinOps economics summary",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "400": error_response,
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
            "/v1/finops/shadow-summary": {
                "get": {
                    "summary": "Summarize shadow policy evaluations",
                    "security": bearer_security,
                    "responses": {
                        "200": {
                            "description": "Shadow policy summary",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
            "/v1/finops/circuits": {
                "get": {
                    "summary": "Inspect provider circuit-breaker states",
                    "security": bearer_security,
                    "responses": {
                        "200": {
                            "description": "Circuit-breaker status snapshot",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
            "/v1/finops/circuits/reset": {
                "post": {
                    "summary": "Reset provider circuit breakers",
                    "security": bearer_security,
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "tier": {
                                            "type": "string",
                                            "enum": ["compact", "mid", "frontier"],
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Circuit-breaker reset confirmation",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "400": error_response,
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
            "/metrics": {
                "get": {
                    "summary": "Prometheus / OpenMetrics exposition",
                    "security": bearer_security,
                    "responses": {
                        "200": {
                            "description": "OpenMetrics text payload",
                            "content": {"text/plain": {"schema": {"type": "string"}}},
                        },
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
            "/dashboard": {
                "get": {
                    "summary": "Live FinOps HTML dashboard",
                    "security": bearer_security,
                    "responses": {
                        "200": {
                            "description": "HTML dashboard page",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        },
                        "401": error_response,
                        "403": error_response,
                    },
                }
            },
        },
    }
