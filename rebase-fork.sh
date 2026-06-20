#!/usr/bin/env bash
# rebase-fork.sh — measure / perform / abort a rebase of the NORA Hermes fork
# (bvisible/hermes-agent, branch version-15) onto the latest upstream release tag.
#
# WHY: our fork carries a handful of NORA commits (deterministic chat pre-router,
# kanban no-progress breaker, desk+WhatsApp delivery layer, mem0 library mode,
# briefing CTA continuity, notifier full-handoff fix, multilingual, …) on top of an
# upstream RELEASE TAG. Upstream moves fast (1000+ commits per release) and refactors
# god-files (gateway/run.py, agent/conversation_loop.py), so we rebase ~monthly. This
# script makes that repeatable AND safe:
#   * works in an ISOLATED git worktree — never touches your main checkout or another
#     session's uncommitted work;
#   * tags a BACKUP of the fork tip before any history rewrite;
#   * leaves `version-15` (what the fleet clones + the nightly ships) UNTOUCHED until
#     you fast-forward it yourself, AFTER validating on Osiris;
#   * can STOP / roll back at any time (--abort).
#
# Usage:
#   ./rebase-fork.sh              # DRY-RUN: lag + conflict surface, no mutation
#   ./rebase-fork.sh --apply      # backup tag + isolated worktree + start the rebase
#   ./rebase-fork.sh --abort      # STOP an in-progress rebase + remove the worktree
#   ./rebase-fork.sh --status     # show worktree / rebase progress
#
# After --apply resolves clean (or you finish resolving the conflicts):
#   git -C <worktree> push origin wip/rebase-<target>
#   # on Osiris, test WITHOUT touching prod (CLAUDE.md):
#   HERMES_REF=wip/rebase-<target> ~/hermes-poc/provision.sh --sync-only
#   # run the det gate + an e2e chat; ONLY then fast-forward version-15:
#   git push origin wip/rebase-<target>:version-15
set -uo pipefail

FORK_REMOTE="${FORK_REMOTE:-origin}"            # bvisible/hermes-agent
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"  # NousResearch/hermes-agent
FORK_BRANCH="${FORK_BRANCH:-version-15}"

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo"; exit 1; }
cd "$REPO"

git fetch "$UPSTREAM_REMOTE" --tags --quiet 2>/dev/null || true
git fetch "$FORK_REMOTE" --quiet 2>/dev/null || true

FORK_TIP="$FORK_REMOTE/$FORK_BRANCH"
# Latest upstream release tag (semver-ish numeric sort over the v2026.* line).
TARGET="${TARGET:-$(git tag -l 'v2026.*' --sort=-version:refname | head -1)}"
# The upstream tag our fork currently sits on (nearest tag reachable from the tip).
BASE="$(git describe --tags --abbrev=0 --match 'v2026.*' "$FORK_TIP" 2>/dev/null || true)"
WIP="wip/rebase-${TARGET}"
WT="$REPO/../hermes-rebase-${TARGET}"
STAMP="$(date +%Y%m%d-%H%M%S)"

measure() {
  echo "fork tip       : $FORK_TIP = $(git rev-parse --short "$FORK_TIP" 2>/dev/null)"
  echo "current base   : ${BASE:-<unknown>}"
  echo "target release : ${TARGET:-<none found>}"
  [ -z "$BASE" ] || [ -z "$TARGET" ] && return 0
  echo "our commits to replay  : $(git rev-list --count --no-merges "$BASE..$FORK_TIP")"
  echo "upstream commits gained: $(git rev-list --count "$BASE..$TARGET")"
  echo ""
  echo "=== CONFLICT SURFACE (our files ∩ upstream-changed files) ==="
  comm -12 <(git diff --name-only "$BASE..$FORK_TIP" | sort) \
           <(git diff --name-only "$BASE..$TARGET"  | sort) | sed 's/^/  /'
  echo "=== our files NOT touched upstream (apply clean) ==="
  comm -23 <(git diff --name-only "$BASE..$FORK_TIP" | sort) \
           <(git diff --name-only "$BASE..$TARGET"  | sort) | sed 's/^/  /'
}

case "${1:-}" in
  ""|--dry-run|-n)
    echo "### DRY-RUN — rebase $FORK_BRANCH (${BASE:-?} → ${TARGET:-?}) ###"
    measure
    echo ""
    echo "Next: ./rebase-fork.sh --apply   (backup tag + isolated worktree, version-15 untouched)"
    ;;
  --apply)
    [ -z "$BASE" ] && { echo "could not detect current base tag"; exit 1; }
    [ "$BASE" = "$TARGET" ] && { echo "already on $TARGET — nothing to rebase"; exit 0; }
    echo "### APPLY — $BASE → $TARGET (isolated worktree, backup tag) ###"
    BK="backup/${FORK_BRANCH}-pre-rebase-${TARGET}-${STAMP}"
    git tag -f "$BK" "$FORK_TIP"
    echo "backup tag : $BK (restore with: git branch -f $FORK_BRANCH $BK)"
    git worktree remove --force "$WT" 2>/dev/null || true
    git worktree add -B "$WIP" "$WT" "$FORK_TIP" >/dev/null
    echo "worktree   : $WT  (branch $WIP)"
    if git -C "$WT" rebase --onto "$TARGET" "$BASE"; then
      echo ""
      echo "✅ REBASE CLEAN."
    else
      echo ""
      echo "⚠️  CONFLICTS — resolve in the isolated worktree (version-15 is untouched):"
      echo "    git -C $WT status"
      echo "    # edit files, then:  git -C $WT add <file> ; git -C $WT rebase --continue"
      echo "    # to STOP & roll back: ./rebase-fork.sh --abort"
    fi
    echo ""
    echo "When clean: push '$WIP', test on Osiris (HERMES_REF=$WIP provision.sh --sync-only),"
    echo "run the det gate + e2e, THEN fast-forward: git push $FORK_REMOTE $WIP:$FORK_BRANCH"
    ;;
  --abort)
    if [ -d "$WT" ]; then
      git -C "$WT" rebase --abort 2>/dev/null || true
      git worktree remove --force "$WT" 2>/dev/null || true
    fi
    git worktree prune 2>/dev/null || true
    echo "STOPPED: rebase aborted, worktree removed. '$FORK_BRANCH' was never touched."
    echo "Backup tags (backup/${FORK_BRANCH}-pre-rebase-*) are kept."
    ;;
  --status)
    echo "=== worktrees ==="; git worktree list
    if [ -d "$WT" ]; then echo "=== $WIP rebase status ==="; git -C "$WT" status | head -10; fi
    ;;
  *) echo "usage: $0 [--dry-run|--apply|--abort|--status]"; exit 1;;
esac
