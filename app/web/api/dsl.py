"""DSL indicator management API backed by the canonical lifecycle engine."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.dsl.engine import DSLEngine

router = APIRouter(prefix="/api/dsl", tags=["dsl"])


class CreateExpressionRequest(BaseModel):
    name: str
    expression: str
    description: str = ""
    direction: str = "none"


class PreviewSingleRequest(BaseModel):
    stock_code: str


def _engine(request: Request) -> DSLEngine:
    """Build the DSL service from the stores owned by the web composition root."""
    return DSLEngine(duck=request.app.state.duck, sqlite=request.app.state.sqlite)


def _require_success(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/expressions")
def list_expressions(request: Request) -> dict[str, list[dict[str, Any]]]:
    expressions = _engine(request).list_all()
    return {
        "expressions": [
            {**expr, "expression": expr["expression_text"]}
            for expr in expressions
        ]
    }


@router.post("/expressions")
def create_expression(
    req: CreateExpressionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return _engine(request).create(
            req.name,
            req.expression,
            req.description,
            req.direction,
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/expressions/{name}/{version}/validate")
def validate_expression(name: str, version: int, request: Request) -> dict[str, Any]:
    return _require_success(_engine(request).validate(name, version))


@router.post("/expressions/{name}/{version}/preview-sample")
def preview_sample(name: str, version: int, request: Request) -> dict[str, Any]:
    return _require_success(_engine(request).preview_sample(name, version))


@router.post("/expressions/{name}/{version}/preview-single")
def preview_single(
    name: str,
    version: int,
    req: PreviewSingleRequest,
    request: Request,
) -> dict[str, Any]:
    return _require_success(_engine(request).preview_single(name, version, req.stock_code))


@router.put("/expressions/{expr_id}/publish")
def publish_expression(expr_id: int, request: Request) -> dict[str, Any]:
    expression = next(
        (item for item in _engine(request).list_all() if item["id"] == expr_id),
        None,
    )
    if expression is None:
        raise HTTPException(status_code=404, detail="expression not found")
    return _require_success(_engine(request).publish(expression["name"], expression["version"]))


@router.delete("/expressions/{expr_id}")
def delete_expression(expr_id: int, request: Request) -> dict[str, str]:
    expression = next(
        (item for item in _engine(request).list_all() if item["id"] == expr_id),
        None,
    )
    if expression is None:
        raise HTTPException(status_code=404, detail="expression not found")
    if expression["status"] == "published":
        raise HTTPException(status_code=400, detail="published expressions are immutable")

    request.app.state.sqlite.execute("DELETE FROM dsl_expressions WHERE id = ?", [expr_id])
    return {"message": "expression deleted"}
