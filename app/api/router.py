from fastapi import APIRouter

from app.api import admin, agents, notify, projects, sprints, tasks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(admin.router)
api_router.include_router(agents.router)
api_router.include_router(agents.directory)
api_router.include_router(agents.join_router)
api_router.include_router(notify.router)
api_router.include_router(projects.router)
api_router.include_router(sprints.router)
api_router.include_router(tasks.router)
