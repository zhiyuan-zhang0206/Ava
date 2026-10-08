import type { DeliveredUploadAcceptance } from "../contracts/types";
import { API_BASE } from "./api-base";

/** Submit one immutable batch. A 202 proves source acceptance, not delivery. */
export function submitUploadedFiles(
  agentId: number,
  files: File[],
  operationKey: string,
  onProgress?: (pct: number) => void,
): Promise<DeliveredUploadAcceptance> {
  if (!Number.isSafeInteger(agentId) || agentId <= 0) throw new Error("invalid upload agent ID");
  if (typeof operationKey !== "string" || !operationKey || operationKey.length > 128) {
    throw new Error("idempotency key must contain 1 to 128 characters");
  }
  if (files.length === 0) throw new Error("upload batch must contain files");
  const submitted = files.map((file) => ({
    file, name: file.name, size: file.size, contentType: file.type || "application/octet-stream",
  }));
  const path = `/api/keyed/v1/agents/${agentId}/uploads`;
  const body = new FormData();
  for (const item of submitted) body.append("files", item.file, item.name);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}${path}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Idempotency-Key", operationKey);
    xhr.setRequestHeader("Idempotency-Scope", "principal-v1");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round(event.loaded / event.total * 100));
    };
    xhr.onload = () => {
      try {
        if (xhr.status !== 202) {
          let detail = `HTTP ${xhr.status}`;
          try {
            const error: unknown = JSON.parse(xhr.responseText);
            if (error && typeof error === "object" && "detail" in error && typeof error.detail === "string") {
              detail = error.detail;
            }
          } catch { /* Keep the actual HTTP status for non-JSON errors. */ }
          throw new Error(detail);
        }
        const accepted: unknown = JSON.parse(xhr.responseText);
        if (!accepted || typeof accepted !== "object" || !("batch_id" in accepted) ||
          typeof accepted.batch_id !== "string" || !/^[0-9a-f]{32}$/.test(accepted.batch_id) ||
          !("agent_id" in accepted) || accepted.agent_id !== agentId ||
          !("status_url" in accepted) || accepted.status_url !== `${path}/${accepted.batch_id}` ||
          !("files" in accepted) || !Array.isArray(accepted.files) || accepted.files.length !== submitted.length) {
          throw new Error("Upload source acceptance is unconfirmed");
        }
        for (const [ordinal, value] of accepted.files.entries()) {
          const item: unknown = value;
          if (!item || typeof item !== "object" || !("ordinal" in item) || item.ordinal !== ordinal ||
            !("filename" in item) || item.filename !== submitted[ordinal].name ||
            !("size" in item) || item.size !== submitted[ordinal].size ||
            !("sha256" in item) || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256) ||
            !("name" in item) || typeof item.name !== "string" ||
            item.name.split(".", 1)[0] !== `${accepted.batch_id}-${ordinal}` ||
            !("content_type" in item) || item.content_type !== submitted[ordinal].contentType) {
            throw new Error("Upload source acceptance is unconfirmed");
          }
        }
        resolve(accepted as DeliveredUploadAcceptance);
      } catch (error) {
        reject(error instanceof Error ? error : new Error("Upload source acceptance is unconfirmed"));
      }
    };
    xhr.onerror = () => reject(new Error("Upload source acceptance is unconfirmed"));
    xhr.send(body);
  });
}
