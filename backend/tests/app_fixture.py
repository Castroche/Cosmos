"""Explicit dependencies shared by legacy integration fixtures, never production globals."""
from cosmos_quant.api import create_app
from cosmos_quant.application_services import ApplicationServices

services = ApplicationServices()
services.app = create_app(services=services)
