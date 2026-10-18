"""Use Gitea's merged PR commit for reviewed code, including retries after a read failure."""
import httpx
import pytest

from app.services.gitea import GiteaClient, GiteaError

BASE = "https://gitea.example.internal"
PULL = f"{BASE}/api/v1/repos/demo/project/pulls/1"
MERGE_SHA = "c" * 40


@pytest.mark.parametrize("style", ["squash", "merge", "rebase"])
@pytest.mark.parametrize("status", [200, 204])
def test_merge_returns_pr_merge_commit(respx_mock, style, status):
    merge = respx_mock.post(f"{PULL}/merge").mock(return_value=httpx.Response(status))
    respx_mock.get(PULL).mock(return_value=httpx.Response(200, json={
        "merged": True, "merge_commit_sha": MERGE_SHA, "head": {"sha": "a" * 40}}))
    assert GiteaClient(BASE, "test-token").merge_pull("demo", "project", 1, style) == MERGE_SHA
    assert f'"Do":"{style}"' in merge.calls[0].request.content.decode()


@pytest.mark.parametrize("pr", [
    {"merged": True}, {"merged": True, "merge_commit_sha": "short"},
    {"merged": False, "merge_commit_sha": MERGE_SHA},
])
def test_merge_does_not_substitute_branch_tip_when_sha_missing(respx_mock, pr):
    respx_mock.post(f"{PULL}/merge").mock(return_value=httpx.Response(204))
    respx_mock.get(PULL).mock(return_value=httpx.Response(200, json={**pr, "head": {"sha": "a" * 40}}))
    with pytest.raises(GiteaError):
        GiteaClient(BASE, "test-token").merge_pull("demo", "project", 1)


def test_retry_recovers_merge_after_failed_sha_read(respx_mock):
    respx_mock.post(f"{PULL}/merge").mock(side_effect=[
        httpx.Response(204), httpx.Response(405, json={"message": "already merged"})])
    respx_mock.get(PULL).mock(side_effect=[
        httpx.Response(503, json={"message": "unavailable"}),
        httpx.Response(200, json={"merged": True, "merge_commit_sha": MERGE_SHA}),
    ])
    client = GiteaClient(BASE, "test-token")
    with pytest.raises(GiteaError):
        client.merge_pull("demo", "project", 1)
    assert client.merge_pull("demo", "project", 1) == MERGE_SHA


@pytest.mark.parametrize("status", [405, 409])
def test_unmerged_conflict_still_raises(respx_mock, status):
    respx_mock.post(f"{PULL}/merge").mock(return_value=httpx.Response(status, json={"message": "conflict"}))
    respx_mock.get(PULL).mock(return_value=httpx.Response(200, json={"merged": False}))
    with pytest.raises(GiteaError) as exc:
        GiteaClient(BASE, "test-token").merge_pull("demo", "project", 1)
    assert exc.value.status == status
