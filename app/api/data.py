"""/api/v1/data/datasets/*/content – local-model agents read `local_only` datasets (research/konzept-datenzonen.md
§11 task 14, docs/data-store.md). `sealed` datasets stay runner/human only and cloud-model agents are refused
(zone helper `zones.may_read_dataset`); every read is written to the data audit before the first byte leaves.

/api/v1/data/results/{id} – the released form of a runner result (output gate, `app/services/output_gate.py`).
Results that were not released do not exist here for anyone; decisions are web-UI only."""
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import SessionDep, current_actor
from app.models import Actor
from app.services import dataset_profiles, datasets, output_gate

router = APIRouter(prefix="/data", tags=["data"])


@router.get("/profiles/{profile_id}")
def profile(profile_id: int, session=SessionDep, me: Actor = Depends(current_actor)):
    import json
    row = dataset_profiles.visible(session, profile_id, me)
    return {"id": row.id, "status": row.status, "schema": json.loads(row.schema_json),
            "sample_csv": row.sample_csv, "schema_fingerprint": row.schema_fingerprint}


@router.get("/datasets/{key}/content")
def content(key: str, session=SessionDep, me: Actor = Depends(current_actor)):
    ds = datasets.get(session, key)
    path, dek = datasets.open_agent_read(session, ds, me)
    name = ds.original_filename or f"{ds.dataset_key}.bin"
    media = ds.media_type or "application/octet-stream"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"}
    session.commit()  # The audit entry is durable before the first decrypted byte leaves.
    return StreamingResponse(datasets.plaintext_chunks(path, dek), media_type=media, headers=headers)


@router.get("/results/{result_id}")
def released_result(result_id: int, session=SessionDep, me: Actor = Depends(current_actor)):
    return output_gate.released_content(session, result_id, me)
