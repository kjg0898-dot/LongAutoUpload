"""업데이트: GitHub에서 최신 프로그램을 받아 프로그램 파일만 바꾼다.
열쇠(client_secret)·로그인(토큰)·설정·백업·기록·영상·완료·엑셀은 받는 파일에 없으므로 건드리지 않는다."""
import io
import os
import urllib.request
import zipfile
from pathlib import Path

# core 등 다른 파일에 기대지 않는다: 다른 파일이 망가져도 이 파일만으로 고친 버전을 받을 수 있게
ROOT = Path(__file__).resolve().parent.parent

URL = "https://codeload.github.com/kjg0898-dot/LongAutoUpload/zip/HEAD"  # 기본 브랜치의 최신 상태


def fetch():
    with urllib.request.urlopen(URL, timeout=60) as res:
        return res.read()


def apply(zip_bytes, root=ROOT, dry=False):
    """받은 zip의 파일 중 바뀐 것만 덮어쓴다(dry면 세기만). 바뀐 파일 수를 돌려준다."""
    changed = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            rel = name.split("/", 1)[1] if "/" in name else ""  # 맨 앞 폴더(LongAutoUpload-HEAD 등)는 뺀다
            if not rel or name.endswith("/") or rel.startswith((".", "docs/", "영상/", "완료/")):  # docs/는 구글 연동용 웹페이지
                continue
            dst, new = root / rel, z.read(name)
            if dst.exists() and dst.read_bytes() == new:
                continue
            changed += 1
            if dry:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".새것")
            tmp.write_bytes(new)
            os.replace(tmp, dst)  # 파일 하나씩 통째로 바꿔서 중간에 끊겨도 반쯤 쓴 파일이 안 남게
    return changed
