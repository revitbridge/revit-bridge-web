"""``/api/skills`` - list and read for everyone; create, edit, import and
delete with ``X-Admin-Token``."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.logs import verify_admin
from backend.skill_store import BUILTIN_PREFIX, get_skill_store

router = APIRouter(prefix="/api/skills", tags=["skills"])

_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com")


class SkillCreateRequest(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0"
    author: str = ""
    enabled: bool = True
    content: str = ""


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    author: str | None = None
    enabled: bool | None = None
    content: str | None = None


class SkillToggleRequest(BaseModel):
    enabled: bool


class SkillImportRequest(BaseModel):
    url: str


@router.get("")
async def list_skills():
    return {"skills": get_skill_store().list_all()}


@router.get("/{skill_id:path}")
async def get_skill(skill_id: str):
    if not skill_id.startswith(BUILTIN_PREFIX) and not re.fullmatch(r"[\w\-]+", skill_id):
        raise HTTPException(400, f"Invalid skill id: {skill_id}")
    skill = get_skill_store().get(skill_id)
    if not skill:
        raise HTTPException(404, f"Skill '{skill_id}' not found")
    return skill


@router.post("", dependencies=[Depends(verify_admin)])
async def create_skill(req: SkillCreateRequest):
    meta = req.model_dump(exclude={"content"})
    return get_skill_store().save(None, meta, req.content)


@router.put("/{skill_id}", dependencies=[Depends(verify_admin)])
async def update_skill(skill_id: str, req: SkillUpdateRequest):
    meta = {k: v for k, v in req.model_dump(exclude={"content"}).items() if v is not None}
    result = get_skill_store().update(skill_id, meta=meta or None, content=req.content)
    if not result:
        raise HTTPException(404, f"Skill '{skill_id}' not found or read-only")
    return result


@router.patch("/{skill_id}", dependencies=[Depends(verify_admin)])
async def toggle_skill(skill_id: str, req: SkillToggleRequest):
    result = get_skill_store().toggle(skill_id, req.enabled)
    if not result:
        raise HTTPException(404, f"Skill '{skill_id}' not found or read-only")
    return result


@router.delete("/{skill_id}", dependencies=[Depends(verify_admin)])
async def delete_skill(skill_id: str):
    if not get_skill_store().delete(skill_id):
        raise HTTPException(404, f"Skill '{skill_id}' not found or read-only")
    return {"status": "ok", "deleted": skill_id}


def candidate_raw_urls(url: str) -> list[str]:
    """Raw-content URLs to try for a GitHub link (repo, blob or raw)."""
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_HOSTS:
        raise HTTPException(400, "Only https GitHub URLs are allowed")
    if parsed.hostname == "raw.githubusercontent.com":
        return [url]
    blob = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", url)
    if blob:
        user, repo, branch, path = blob.groups()
        return [f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"]
    repo_only = re.match(r"https://github\.com/([^/]+)/([^/]+)$", url)
    if repo_only:
        user, repo = repo_only.groups()
        base = f"https://raw.githubusercontent.com/{user}/{repo}"
        return [
            f"{base}/main/skills/{repo}/SKILL.md",
            f"{base}/master/skills/{repo}/SKILL.md",
            f"{base}/main/SKILL.md",
            f"{base}/master/SKILL.md",
            f"{base}/main/README.md",
        ]
    raise HTTPException(400, "Unsupported GitHub URL; use a repo, blob or raw link")


@router.post("/import", dependencies=[Depends(verify_admin)])
async def import_skill(req: SkillImportRequest):
    urls = candidate_raw_urls(req.url)
    text, used = None, ""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for candidate in urls:
            try:
                resp = await client.get(candidate)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                text, used = resp.text, candidate
                break
    if text is None:
        raise HTTPException(400, f"Could not fetch a skill from {req.url}; tried {', '.join(urls[:3])}")
    result = get_skill_store().import_from_text(text, source=req.url)
    return {**result, "imported_from": used}
