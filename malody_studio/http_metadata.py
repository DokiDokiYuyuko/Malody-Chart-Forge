"""Validate user metadata without treating an explicitly empty form as absent."""
from fastapi import Form, HTTPException, Request
from .naming import DEFAULT_CHART_CREATOR, validate_creator


async def creator_form(request: Request, creator: str = Form(DEFAULT_CHART_CREATOR)):
    # FastAPI applies Form defaults to empty strings. Read the cached form to
    # distinguish an omitted setting from an explicitly cleared input.
    form = await request.form()
    try:
        return validate_creator(form.get('creator', creator))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
