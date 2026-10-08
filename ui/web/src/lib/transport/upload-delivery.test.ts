import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { submitUploadedFiles } from "./upload-delivery";

class UploadXHR {
  static instances: UploadXHR[] = [];
  open = vi.fn();
  send = vi.fn();
  setRequestHeader = vi.fn();
  withCredentials = false;
  status = 202;
  responseText = "";
  upload: { onprogress?: (event: ProgressEvent) => void } = {};
  onload?: () => void;
  onerror?: () => void;
  constructor() { UploadXHR.instances.push(this); }
}

const batchId = "a".repeat(32);
const file = new File(["hello"], "report.txt", { type: "text/plain" });
const acceptance = () => ({
  batch_id: batchId,
  agent_id: 7,
  status_url: `/api/keyed/v1/agents/7/uploads/${batchId}`,
  files: [{ ordinal: 0, filename: "report.txt", name: `${batchId}-0.txt`, size: 5,
    sha256: "b".repeat(64), content_type: "text/plain" }],
});

beforeEach(() => {
  UploadXHR.instances = [];
  vi.stubGlobal("XMLHttpRequest", UploadXHR);
});
afterEach(() => vi.unstubAllGlobals());

describe("delivered upload source acceptance", () => {
  it("submits once to the guarded path and completes at source acceptance", async () => {
    const progress = vi.fn();
    const files = [file];
    const result = submitUploadedFiles(7, files, "original-intent", progress);
    files.length = 0;
    const xhr = UploadXHR.instances[0];
    expect(xhr.open).toHaveBeenCalledWith("POST", expect.stringMatching(/\/api\/keyed\/v1\/agents\/7\/uploads$/));
    expect(xhr.withCredentials).toBe(true);
    expect(xhr.setRequestHeader.mock.calls).toEqual([
      ["Idempotency-Key", "original-intent"], ["Idempotency-Scope", "principal-v1"],
    ]);
    const body = xhr.send.mock.calls[0][0] as FormData;
    expect(body.getAll("files")).toHaveLength(1);
    expect((body.get("files") as File).name).toBe("report.txt");
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 5 } as ProgressEvent);
    expect(progress).toHaveBeenCalledWith(100);
    xhr.responseText = JSON.stringify(acceptance());
    xhr.onload?.();
    await expect(result).resolves.toEqual(acceptance());
    expect(UploadXHR.instances).toHaveLength(1);
  });

  it.each([200, 404, 409, 503])("never falls back or resubmits on HTTP %i", async (status) => {
    const result = submitUploadedFiles(7, [file], "original-intent");
    const assertion = expect(result).rejects.toThrow(`HTTP ${status}`);
    const xhr = UploadXHR.instances[0];
    xhr.status = status;
    xhr.responseText = JSON.stringify(acceptance());
    xhr.onload?.();
    await assertion;
    expect(UploadXHR.instances).toHaveLength(1);
    expect(xhr.send).toHaveBeenCalledTimes(1);
  });

  it("preserves the explicit identity for a deliberate same-intent resubmission", async () => {
    const first = submitUploadedFiles(7, [file], "original-intent");
    const failure = expect(first).rejects.toThrow("unconfirmed");
    UploadXHR.instances[0].onerror?.();
    await failure;
    expect(UploadXHR.instances).toHaveLength(1);
    const second = submitUploadedFiles(7, [file], "original-intent");
    const replay = UploadXHR.instances[1];
    expect(replay.setRequestHeader).toHaveBeenCalledWith("Idempotency-Key", "original-intent");
    replay.responseText = JSON.stringify(acceptance());
    replay.onload?.();
    await expect(second).resolves.toEqual(acceptance());
  });

  it.each([
    { ...acceptance(), agent_id: 8 },
    { ...acceptance(), status_url: "/api/agents/7/uploads" },
    { ...acceptance(), files: [] },
    { ...acceptance(), files: [{ ...acceptance().files[0], filename: "other.txt" }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], size: 4 }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], content_type: "image/png" }] },
    { ...acceptance(), files: [{ ...acceptance().files[0], name: `${batchId}-00.txt` }] },
  ])("rejects a mismatched source receipt", async (receipt) => {
    const result = submitUploadedFiles(7, [file], "original-intent");
    const failure = expect(result).rejects.toThrow("unconfirmed");
    UploadXHR.instances[0].responseText = JSON.stringify(receipt);
    UploadXHR.instances[0].onload?.();
    await failure;
  });

  it("settles malformed JSON without leaving the caller pending", async () => {
    const result = submitUploadedFiles(7, [file], "original-intent");
    const failure = expect(result).rejects.toThrow();
    UploadXHR.instances[0].responseText = "{";
    UploadXHR.instances[0].onload?.();
    await failure;
  });

  it("matches the multipart default for a File without a MIME type", async () => {
    const untyped = new File(["hello"], "report.txt");
    const result = submitUploadedFiles(7, [untyped], "original-intent");
    const receipt = acceptance();
    receipt.files[0].content_type = "application/octet-stream";
    UploadXHR.instances[0].responseText = JSON.stringify(receipt);
    UploadXHR.instances[0].onload?.();
    await expect(result).resolves.toEqual(receipt);
  });

  it("rejects invalid identity and empty batches before transport effects", () => {
    expect(() => submitUploadedFiles(0, [file], "key")).toThrow("agent ID");
    expect(() => submitUploadedFiles(7, [file], "")).toThrow("idempotency key");
    expect(() => submitUploadedFiles(7, [file], "x".repeat(129))).toThrow("idempotency key");
    expect(() => submitUploadedFiles(7, [], "key")).toThrow("contain files");
    expect(UploadXHR.instances).toHaveLength(0);
  });
});
