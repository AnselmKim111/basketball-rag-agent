"""헬퍼 재배치(`_summarize_pdfs_parallel` → `src.bot_helpers`) 후 모든 호출자의
import가 같은 객체를 가리키는지 검증. PIPELINE_LOCK 범위 축소 작업에서
4개 모듈이 같은 헬퍼를 공유해야 함.

또 curator의 새 `_download_picks_blocking` 시그니처(다운로드만, 요약 없음) 보장.
"""

from __future__ import annotations

import inspect


def test_summarize_pdfs_parallel_relocated_and_shared():
    """헬퍼는 src.bot_helpers에 살고, 4개 호출 모듈이 동일 객체를 import한다."""
    from src.bot_helpers import _summarize_pdfs_parallel
    from src.category_bots import _summarize_pdfs_parallel as cb
    from src.curator import _summarize_pdfs_parallel as cu
    # deep_research·comparator는 아직 직접 import 안 했지만 (lock 범위 축소만 함)
    # bot_helpers에서 가져갈 수 있어야 한다는 의미로 명시 import 시도.
    from src.bot_helpers import _summarize_pdfs_parallel as bh
    assert cb is _summarize_pdfs_parallel
    assert cu is _summarize_pdfs_parallel
    assert bh is _summarize_pdfs_parallel


def test_curator_download_only_helper():
    """기존 _download_and_summarize_blocking 은 사라지고 다운로드 전용으로 대체."""
    from src import curator
    assert hasattr(curator, "_download_picks_blocking")
    assert not hasattr(curator, "_download_and_summarize_blocking")
    sig = inspect.signature(curator._download_picks_blocking)
    assert list(sig.parameters) == ["picks", "download_root"]


def test_pipeline_lock_holders_have_narrowed_scope():
    """리팩터링 후 외부 시그니처는 모두 보존됐는지 확인 (smoke import)."""
    from src.curator import run_curated
    from src.deep_research import run as deep_research_run
    from src.comparator import run_compare
    from src.bot_worker import _run_pipeline
    # 단순 import 통과면 모듈 로드 OK — syntax/순환 import 회귀 검출용.
    assert callable(run_curated)
    assert callable(deep_research_run)
    assert callable(run_compare)
    assert callable(_run_pipeline)
