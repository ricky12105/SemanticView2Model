"""Fabric REST + Snowflake deployment helpers."""
from translator.deploy.fabric_client import deploy_semantic_model, get_semantic_model
from translator.deploy.snowflake_client import execute_ddl

__all__ = ["deploy_semantic_model", "get_semantic_model", "execute_ddl"]
