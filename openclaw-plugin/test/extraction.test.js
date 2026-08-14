import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";

import { createTranscriptionExtractor } from "../extraction.js";


describe("roster structured extraction", () => {
  it("uses the media-understanding structured seam once for all images", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "wife-roster-plugin-"));
    const first = path.join(directory, "first.png");
    const second = path.join(directory, "second.jpg");
    const promptPath = path.join(directory, "prompt.md");
    await writeFile(first, Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.from("one")]));
    await writeFile(second, Buffer.concat([Buffer.from("ffd8ff", "hex"), Buffer.from("two")]));
    await writeFile(promptPath, "Transcribe printed roster fields only.");
    const calls = [];
    const api = {
      config: { agents: { defaults: { model: { primary: "openai/text-only" } } } },
      runtime: { mediaUnderstanding: {
        extractStructuredWithModel: async (value) => {
          calls.push(value);
          return { text: JSON.stringify({ schema_version: 1, coverage: "FULL", report_header: { period_from: null, period_to: null, port_local_notice_present: true }, rows: [] }) };
        },
      } },
    };
    const extractor = createTranscriptionExtractor(api, {
      promptPath,
      extractionModel: "openai/gpt-5.6-sol",
      extractionTimeoutMs: 1000,
    });

    const result = await extractor([
      { path: first, contentType: "image/png" },
      { path: second, contentType: "image/jpeg" },
    ]);

    assert.equal(result.coverage, "FULL");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].provider, "openai");
    assert.equal(calls[0].model, "gpt-5.6-sol");
    assert.equal(calls[0].input.filter((item) => item.type === "image").length, 2);
    assert.equal("agent" in api.runtime, false);
    assert.equal("subagent" in api.runtime, false);
  });

  it("fails closed instead of inheriting the general agent model", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "wife-roster-plugin-"));
    const imagePath = path.join(directory, "roster.png");
    const promptPath = path.join(directory, "prompt.md");
    await writeFile(
      imagePath,
      Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.from("test")]),
    );
    await writeFile(promptPath, "Transcribe printed roster fields only.");
    let called = false;
    const api = {
      config: { agents: { defaults: { model: { primary: "openai/text-only" } } } },
      runtime: { mediaUnderstanding: {
        extractStructuredWithModel: async () => {
          called = true;
          return { text: "{}" };
        },
      } },
    };
    const extractor = createTranscriptionExtractor(api, { promptPath });

    await assert.rejects(
      () => extractor([{ path: imagePath, contentType: "image/png" }]),
      { name: "TranscriptionExtractionError" },
    );
    assert.equal(called, false);
  });
});
