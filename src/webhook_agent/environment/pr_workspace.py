"""PR Head Workspace Context Manager.

Downloads the repository tarball for a PR head SHA, safely extracts it into an
ephemeral sandboxed environment, and cleans up on exit.
"""

from __future__ import annotations

import contextlib
import logging
import tarfile
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from webhook_agent.constants import ENABLE_PR_WORKSPACE
from webhook_agent.environment.base import BaseEnvironment
from webhook_agent.environment.context import ephemeral_environment

logger = logging.getLogger("webhook_agent.environment.pr_workspace")

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_FILES = 20_000
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024  # 200 MB


@contextlib.asynccontextmanager
async def pr_workspace(
    gh: Any,
    repo_full_name: str,
    head_sha: str,
) -> AsyncIterator[BaseEnvironment | None]:
    """Download PR head tarball, extract into ephemeral sandbox, and yield environment.

    Safeguards:
    - Env kill switch ENABLE_PR_WORKSPACE (default 1). If 0, yields None without error.
    - Caps archive download stream at 100 MB.
    - Strips the single top-level directory from the archive.
    - Drops/rejects symlink and hardlink members.
    - Caps extracted contents at 20,000 files and 200 MB.
    - Uses tarfile filter="data".
    - Never raises on setup/download failure; logs warning and yields None.
    - Cleans up ephemeral files upon exit.
    """
    enable = ENABLE_PR_WORKSPACE.strip().lower()
    if enable in ("0", "false", "no", "off"):
        logger.info("PR workspace disabled via ENABLE_PR_WORKSPACE=0")
        yield None
        return

    if not gh or not repo_full_name or not head_sha:
        logger.info("Skipping PR workspace setup: missing head SHA or PR context")
        yield None
        return

    logger.info("Beginning PR workspace download and setup for %s@%s", repo_full_name, head_sha)

    try:
        repo = gh.get_repo(repo_full_name)
        archive_url = repo.get_archive_link("tarball", ref=head_sha)
    except Exception as link_err:
        logger.warning(
            "Could not obtain archive download link for %s@%s: %s",
            repo_full_name,
            head_sha,
            link_err,
        )
        yield None
        return

    with tempfile.TemporaryFile(mode="w+b") as temp_archive:
        try:
            downloaded = 0
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                async with client.stream("GET", archive_url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Archive download failed with HTTP %d for %s@%s",
                            response.status_code,
                            repo_full_name,
                            head_sha,
                        )
                        yield None
                        return
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_BYTES:
                            logger.warning(
                                "Archive for %s@%s exceeded 100 MB download cap (downloaded %d bytes); aborting workspace setup",
                                repo_full_name,
                                head_sha,
                                downloaded,
                            )
                            yield None
                            return
                        temp_archive.write(chunk)

            temp_archive.seek(0)
        except Exception as fetch_err:
            logger.warning(
                "PR workspace download failed for %s@%s: %s",
                repo_full_name,
                head_sha,
                fetch_err,
            )
            yield None
            return

        short_sha = head_sha[:7] if head_sha else "ephemeral"
        sandbox_prefix = f"hannibal-sandbox-{short_sha}-"
        async with ephemeral_environment(prefix=sandbox_prefix) as env:
            extraction_ok = False
            file_count = 0
            extracted_bytes = 0
            try:
                with tarfile.open(fileobj=temp_archive, mode="r:*") as tar:
                    members: list[tarfile.TarInfo] = []
                    for m in tar.getmembers():
                        # Reject symlinks and hardlinks
                        if m.issym() or m.islnk():
                            continue
                        parts = Path(m.name).parts
                        if len(parts) <= 1 or ".." in parts or Path(m.name).is_absolute():
                            # Skip root directory, traversal members, or absolute paths
                            continue
                        # Strip the top-level directory
                        m.name = str(Path(*parts[1:]))
                        if m.isfile():
                            file_count += 1
                            extracted_bytes += m.size
                        if file_count > MAX_FILES:
                            raise ValueError(
                                f"Archive exceeded max file count limit of {MAX_FILES} (found {file_count})"
                            )
                        if extracted_bytes > MAX_EXTRACTED_BYTES:
                            raise ValueError(
                                f"Archive exceeded max extracted size limit of 200 MB (total {extracted_bytes} bytes)"
                            )
                        members.append(m)

                    tar.extractall(path=env.root, members=members, filter="data")
                extraction_ok = True
            except Exception as extract_err:
                logger.warning(
                    "PR workspace extraction failed for %s@%s: %s",
                    repo_full_name,
                    head_sha,
                    extract_err,
                )

            if extraction_ok:
                logger.info(
                    "PR workspace extracted successfully for %s@%s: %d files, %d bytes at %s",
                    repo_full_name,
                    head_sha,
                    file_count,
                    extracted_bytes,
                    env.root,
                )
                try:
                    yield env
                finally:
                    logger.info(
                        "Cleaning up PR workspace for %s@%s at %s",
                        repo_full_name,
                        head_sha,
                        env.root,
                    )
            else:
                yield None
