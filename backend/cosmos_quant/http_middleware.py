"""Resolve CORS from initialized application state, never import-time environment reads."""

from starlette.middleware.cors import CORSMiddleware


class InstanceCORSMiddleware:
    def __init__(self, app):
        self.app = app
        self.http_middleware = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.http_middleware is None:
            services = scope["app"].state.services
            origins = services.env_settings.cors_origins if services else ["http://localhost:5173"]
            self.http_middleware = CORSMiddleware(
                self.app,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        await self.http_middleware(scope, receive, send)
