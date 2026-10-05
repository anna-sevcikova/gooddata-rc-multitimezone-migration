from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import requests


API_MEDIA = "application/vnd.gooddata.api+json"


class ApiError(RuntimeError):
    pass


COLLECTION_BY_CATEGORY = {
    "metric": "metrics",
    "visualization": "visualizationObjects",
    "dashboard": "analyticalDashboards",
}

TYPE_BY_CATEGORY = {
    "metric": "metric",
    "visualization": "visualizationObject",
    "dashboard": "analyticalDashboard",
}


@dataclass
class GoodDataApi:
    host: str
    workspace: str
    token: str
    timeout_connect: int = 10
    timeout_read: int = 90

    def __post_init__(self) -> None:
        self.host = self.host.rstrip("/")
        if not self.host:
            raise ApiError("Host is empty")
        if not self.workspace:
            raise ApiError("Workspace ID is empty")
        if not self.token:
            raise ApiError("Token is empty")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": API_MEDIA,
        }

    def entity_url(self, collection: str, object_id: str | None = None) -> str:
        base = f"{self.host}/api/v1/entities/workspaces/{self.workspace}/{collection}"
        return f"{base}/{object_id}" if object_id else base

    def action_url(self, action: str) -> str:
        return f"{self.host}/api/v1/actions/workspaces/{self.workspace}/{action}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        content_type: str | None = None,
        accept: str | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> requests.Response:
        headers = dict(self.headers)
        if accept is not None:
            headers["Accept"] = accept
        if payload is not None:
            headers["Content-Type"] = content_type or API_MEDIA
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=(self.timeout_connect, self.timeout_read),
        )
        if response.status_code not in expected:
            body = response.text[:4000]
            raise ApiError(
                f"{method} {url} failed: HTTP {response.status_code}: {body}"
            )
        return response

    def dependent_entities_graph(
        self,
        identifiers: list[dict[str, str]],
        *,
        relation: str = "DEPENDENTS",
    ) -> dict[str, Any]:
        """Cloud Action API equivalent of Platform used-by for entry-point IDs."""
        if not identifiers:
            raise ApiError("dependentEntitiesGraph requires at least one identifier")
        # Action APIs speak application/json, not the Entity API media type.
        response = self._request(
            "POST",
            self.action_url("dependentEntitiesGraph"),
            payload={"identifiers": identifiers, "relation": relation},
            content_type="application/json",
            accept="application/json",
        )
        try:
            return response.json()
        except Exception as exc:
            raise ApiError("dependentEntitiesGraph returned non-JSON content") from exc

    def get_entity(self, collection: str, object_id: str) -> dict[str, Any]:
        response = self._request("GET", self.entity_url(collection, object_id))
        try:
            return response.json()
        except Exception as exc:
            raise ApiError(
                f"GET {collection}/{object_id} returned non-JSON content"
            ) from exc

    def try_get_entity(self, collection: str, object_id: str) -> dict[str, Any] | None:
        url = self.entity_url(collection, object_id)
        response = requests.get(
            url,
            headers=self.headers,
            timeout=(self.timeout_connect, self.timeout_read),
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ApiError(
                f"GET {url} failed: HTTP {response.status_code}: {response.text[:4000]}"
            )
        try:
            return response.json()
        except Exception as exc:
            raise ApiError(f"GET {url} returned non-JSON content") from exc

    def list_entities(
        self, collection: str, page_size: int = 200, *, origin: str | None = None
    ) -> list[dict[str, Any]]:
        """List a collection. ``origin="NATIVE"`` returns only objects owned by this
        workspace (skips objects inherited from a parent, which a child cannot edit)."""
        out: list[dict[str, Any]] = []
        page = 0
        while True:
            params: dict[str, Any] = {"page": page, "size": page_size}
            if origin:
                params["origin"] = origin
            response = self._request(
                "GET",
                self.entity_url(collection),
                params=params,
            )
            try:
                payload = response.json()
            except Exception as exc:
                raise ApiError(f"GET collection {collection} returned non-JSON content") from exc
            data = payload.get("data")
            if not isinstance(data, list):
                raise ApiError(f"GET collection {collection}: response.data is not a list")
            out.extend(x for x in data if isinstance(x, dict))
            if len(data) < page_size:
                break
            page += 1
            if page > 10000:
                raise ApiError(f"Pagination guard exceeded for collection {collection}")
        return out

    def put_entity(self, collection: str, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PUT",
            self.entity_url(collection, object_id),
            payload=payload,
            expected=(200,),
        )
        try:
            return response.json()
        except Exception:
            return {}


def entity_data(entity: dict[str, Any]) -> dict[str, Any]:
    data = entity.get("data")
    if not isinstance(data, dict):
        raise ApiError("Entity payload has no data object")
    return data


def entity_attributes(entity: dict[str, Any]) -> dict[str, Any]:
    attrs = entity_data(entity).get("attributes")
    if not isinstance(attrs, dict):
        raise ApiError("Entity payload has no data.attributes object")
    return attrs


def entity_content(entity: dict[str, Any]) -> dict[str, Any]:
    content = entity_attributes(entity).get("content")
    if not isinstance(content, dict):
        raise ApiError("Entity payload has no attributes.content object")
    return content


def entity_id(entity: dict[str, Any]) -> str:
    return str(entity_data(entity).get("id") or "")


def entity_type(entity: dict[str, Any]) -> str:
    return str(entity_data(entity).get("type") or "")


def entity_title(entity: dict[str, Any]) -> str:
    return str(entity_attributes(entity).get("title") or "")


def put_payload_from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    data = entity_data(entity)
    return {
        "data": {
            "id": data.get("id"),
            "type": data.get("type"),
            "attributes": copy.deepcopy(entity_attributes(entity)),
        }
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
