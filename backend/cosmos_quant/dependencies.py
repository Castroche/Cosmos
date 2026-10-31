from typing import Annotated, Any

from fastapi import Depends, Request


def get_services(request: Request):
    services = request.app.state.services
    if services is None:
        raise RuntimeError("Application lifespan has not initialized services")
    return services


ServiceDependency = Annotated[Any, Depends(get_services)]
