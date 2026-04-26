"""Deepdive 서브패키지.

격리 원칙: 패키지 import 자체는 부작용 0.
무거운 의존성(matplotlib, dart_fss)은 모두 handler.py 내 함수 본문에서 import.
"""
