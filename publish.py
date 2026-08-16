"""화면 파일을 gh-pages 갈래에 올린다.

  python publish.py            # 오늘 만들어진 public/index.html 을 올린다
  python publish.py --setup    # gh-pages 갈래를 처음 만든다(한 번만)
  python publish.py --dry-run  # 갈래에 파일만 놓고 커밋·푸시는 안 한다

왜 갈래를 나누나
  main 은 코드·문서, gh-pages 는 화면 파일만 담는다. 화면 파일은 매일 바뀌므로
  main 에 두면 코드 이력이 자동 커밋으로 덮인다. `--orphan` 으로 만들어
  main 과 공통 조상이 없다(합칠 일이 없다).

왜 worktree 를 쓰나
  갈래를 오가면(`git checkout`) 작업 폴더의 수집 데이터가 매번 바뀐다. 새벽에
  수집이 도는 중이면 그 사이에 파일이 사라진다. worktree 는 별도 폴더에
  gh-pages 를 펼치므로 본 작업 폴더를 안 건드린다.

★푸시 실패는 수집 실패가 아니다. run.py 는 이 파일의 실패를 로그와 확인 필요
  표시로만 남긴다. 값은 이미 JSON·DB에 들어가 있고 나중에 손으로 다시 올릴 수 있다.

★이 파일에는 자체 중복 실행 잠금이 없다. 매일 실행은 run.py 의 잠금 안에서
  돌아 겹치지 않는다. 손으로 두 개를 동시에 돌리면 한쪽이 git 의 ref 잠금 오류로
  실패하는데, 값이 망가지지는 않고 오류가 그대로 보인다(2026-08-01 재현 확인).
"""
import argparse
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
BRANCH = "gh-pages"
WORKTREE = os.path.join(BASE, ".gh-pages")      # .gitignore 에 넣어 둔다
SOURCE = os.path.join(BASE, "public", "index.html")

# 검색엔진에게 전부 막는다고 알린다. 화면 파일 안의 noindex 표시와 두 겹
# (2026-07-30 결정: 주소를 아는 사람은 볼 수 있게 두되 검색에는 안 뜨게).
ROBOTS = "User-agent: *\nDisallow: /\n"

# gh-pages 갈래 안에 두는 무시 목록. 이 갈래에는 화면 파일만 둔다.
# publish() 는 파일 이름을 하나씩 지정해 add 하지만, 사람이 그 폴더에 들어가
# `git add -A` 를 치면 거기 있던 아무 파일이나 공개 갈래로 올라간다.
# 전부 막고 아래 넷만 연다.
PAGES_IGNORE = "*\n!index.html\n!robots.txt\n!.nojekyll\n!.gitignore\n"


class PublishError(Exception):
    """올리기 실패. 부르는 쪽이 수집을 죽이지 않고 넘어가라고 구분해 둔 예외."""


def git(*args, cwd=BASE, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PublishError(f"git {' '.join(args)} 실패: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def has_remote():
    return bool(git("remote", check=False))


def branch_exists(name):
    return git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
               check=False) != ""


def remote_branch_exists(name):
    return git("ls-remote", "--heads", "origin", name, check=False) != ""


def setup():
    """gh-pages 갈래와 worktree 를 만든다. 이미 있으면 그대로 둔다."""
    if os.path.isdir(WORKTREE) and os.path.exists(os.path.join(WORKTREE, ".git")):
        return f"worktree 이미 있음: {WORKTREE}"
    if os.path.isdir(WORKTREE):
        # 폴더는 있는데 worktree 가 아닌 경우. 그냥 두면 git 이 'already exists' 라고만
        # 하고 끝나 원인을 못 찾는다. 사람이 지우거나 옮겨야 하는 상황이라 그렇게 알린다
        raise PublishError(
            f"{WORKTREE} 폴더가 있는데 gh-pages worktree 가 아니다. "
            f"안을 확인하고 지운 뒤(`rm -rf {WORKTREE}`) 다시 --setup 해라")
    if branch_exists(BRANCH):
        git("worktree", "add", WORKTREE, BRANCH)
        return f"기존 {BRANCH} 갈래를 {WORKTREE} 에 펼침"
    if has_remote() and remote_branch_exists(BRANCH):
        git("fetch", "origin", f"{BRANCH}:{BRANCH}")
        git("worktree", "add", WORKTREE, BRANCH)
        return f"원격 {BRANCH} 를 받아 {WORKTREE} 에 펼침"
    # 처음 만드는 경우. --orphan 이라 main 과 이력을 안 나눈다
    git("worktree", "add", "--detach", WORKTREE)
    git("checkout", "--orphan", BRANCH, cwd=WORKTREE)
    git("rm", "-rf", "--quiet", ".", cwd=WORKTREE, check=False)
    return f"{BRANCH} 갈래를 새로 만듦: {WORKTREE}"


def stage_files(date_str):
    """worktree 에 화면 파일·robots.txt·.nojekyll 을 놓는다. 반환: 놓은 파일 목록."""
    if not os.path.exists(SOURCE):
        raise PublishError(f"올릴 화면 파일이 없다: {SOURCE}")
    shutil.copyfile(SOURCE, os.path.join(WORKTREE, "index.html"))
    with open(os.path.join(WORKTREE, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(ROBOTS)
    # .nojekyll 이 없으면 GitHub Pages 가 Jekyll 로 한 번 더 처리한다.
    # 지금 화면은 완성된 HTML 한 장이라 그 처리가 필요 없다
    open(os.path.join(WORKTREE, ".nojekyll"), "w").close()
    with open(os.path.join(WORKTREE, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(PAGES_IGNORE)
    return ["index.html", "robots.txt", ".nojekyll", ".gitignore"]


def publish(date_str, dry_run=False):
    """반환: (올렸나, 사람이 읽을 한 줄)."""
    note = setup()
    files = stage_files(date_str)
    if dry_run:
        return False, f"{note} · 파일 {len(files)}개 놓기만 함(커밋·푸시 안 함)"

    git("add", *files, cwd=WORKTREE)
    if not git("status", "--porcelain", cwd=WORKTREE):
        return False, "화면 파일이 어제와 같아 올릴 것이 없음"
    git("commit", "-m", f"화면 갱신 {date_str}", cwd=WORKTREE)
    if not has_remote():
        return False, f"커밋함. 원격이 없어 푸시는 안 함(갈래 {BRANCH})"
    git("push", "-u", "origin", BRANCH, cwd=WORKTREE)
    return True, f"{BRANCH} 갈래에 올림 ({date_str})"


def main():
    ap = argparse.ArgumentParser(description="화면 파일을 gh-pages 갈래에 올린다")
    ap.add_argument("date", nargs="?", default="", help="커밋 메시지에 적을 날짜")
    ap.add_argument("--setup", action="store_true", help="갈래만 만들고 끝낸다")
    ap.add_argument("--dry-run", action="store_true", help="커밋·푸시 없이 준비만")
    args = ap.parse_args()

    date_str = args.date or __import__("datetime").date.today().isoformat()
    try:
        if args.setup:
            print("[올리기]", setup())
            return 0
        ok, note = publish(date_str, dry_run=args.dry_run)
        print("[올리기]", note)
        return 0 if ok or args.dry_run or "올릴 것이 없음" in note else 1
    except PublishError as e:
        print("[올리기] 실패:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
