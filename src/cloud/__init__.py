"""
src/cloud/__init__.py
Cloud server execution subsystem for Manga Recap (Kaggle, Colab, Cloud GPUs).
"""

from src.cloud.kaggle_service import KaggleCloudService

__all__ = ["KaggleCloudService"]
