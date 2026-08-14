import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { describe, it } from "node:test";

import { createWorkflowRunner } from "../workflow.js";


const REVIEW_ID = "AbCdEfGhIjKlMnOp";


function fakeSpawn(capture, response, exitCode = 0) {
  return (binary, args, options) => {
    capture.binary = binary;
    capture.args = args;
    capture.options = options;
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = {
      end(value) {
        capture.stdin = value;
        queueMicrotask(() => {
          child.stdout.emit("data", Buffer.from(JSON.stringify(response)));
          child.emit("close", exitCode, null);
        });
      },
    };
    child.kill = () => {};
    return child;
  };
}

describe("wife-roster workflow subprocess", () => {
  it("uses direct Python module execution with shell disabled and JSON stdin", async () => {
    const capture = {};
    const runner = createWorkflowRunner({
      pythonBin: "/absolute/venv/bin/python",
      appSrc: "/absolute/app/src",
      vendorPath: "/absolute/app/vendor",
      workingDirectory: "/absolute/runtime",
      workflowTimeoutMs: 1000,
    }, fakeSpawn(capture, { handled: true, ok: true, reply: "reviewed" }));
    const payload = { group_id: "-1", sender_id: "2", attachments: [] };

    const result = await runner("review", payload);

    assert.equal(result.reply, "reviewed");
    assert.equal(capture.binary, "/absolute/venv/bin/python");
    assert.deepEqual(capture.args, ["-m", "roster.cli", "inbound-review"]);
    assert.equal(capture.options.shell, false);
    assert.equal(capture.options.cwd, "/absolute/runtime");
    assert.deepEqual(JSON.parse(capture.stdin), payload);
    assert.equal(capture.args.includes("agent"), false);
    assert.equal(capture.args.includes("model"), false);
  });

  it("uses the separate deterministic approval command", async () => {
    const capture = {};
    const runner = createWorkflowRunner({
      pythonBin: "/absolute/venv/bin/python",
      appSrc: "/absolute/app/src",
      vendorPath: "/absolute/app/vendor",
      workingDirectory: "/absolute/runtime",
      workflowTimeoutMs: 1000,
    }, fakeSpawn(capture, { handled: true, ok: false, reply: "No roster awaiting approval." }, 2));

    const result = await runner("approve", { group_id: "-1", sender_id: "2" });

    assert.equal(result.reply, "No roster awaiting approval.");
    assert.deepEqual(capture.args, ["-m", "roster.cli", "inbound-approve"]);
  });

  it("uses the separate deterministic revision command without an agent or model", async () => {
    const capture = {};
    const runner = createWorkflowRunner({
      pythonBin: "/absolute/venv/bin/python",
      appSrc: "/absolute/app/src",
      vendorPath: "/absolute/app/vendor",
      workingDirectory: "/absolute/runtime",
      workflowTimeoutMs: 1000,
    }, fakeSpawn(capture, { handled: true, ok: true, reply: "Roster not activated." }));
    const payload = { review_id: REVIEW_ID, group_id: "-1", sender_id: "2" };

    const result = await runner("revise", payload);

    assert.equal(result.reply, "Roster not activated.");
    assert.deepEqual(capture.args, ["-m", "roster.cli", "inbound-revise"]);
    assert.deepEqual(JSON.parse(capture.stdin), payload);
    assert.equal(capture.options.shell, false);
    assert.equal(capture.args.includes("agent"), false);
    assert.equal(capture.args.includes("model"), false);
  });
});
