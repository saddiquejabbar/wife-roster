import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { createInboundController } from "../controller.js";


const GROUP = "test-group";
const OWNER = "test-owner";
const REVIEWER = "test-reviewer";
const REVIEW_ID = "AbCdEfGhIjKlMnOp";

function context(overrides = {}) {
  return {
    Provider: "telegram",
    Surface: "telegram",
    ChatType: "group",
    ChatId: GROUP,
    OriginatingTo: `telegram:${GROUP}`,
    SenderId: OWNER,
    GroupRequireMention: true,
    ExplicitlyMentionedBot: true,
    WasMentioned: true,
    MentionSource: "explicit_bot",
    CommandBody: "run wife-roster",
    MediaPaths: ["/private/roster.png"],
    MediaTypes: ["image/png"],
    ...overrides,
  };
}

function harness() {
  const calls = { extraction: [], workflow: [] };
  const controller = createInboundController({
    config: {
      groupId: GROUP,
      allowedSenderIds: [OWNER],
      botUsername: "RosterDemoBot",
    },
    extractTranscription: async (attachments) => {
      calls.extraction.push(attachments);
      return { schema_version: 1, report_header: {}, rows: [] };
    },
    runWorkflow: async (command, payload) => {
      calls.workflow.push({ command, payload });
      return { handled: true, ok: true, reply: `${command} complete` };
    },
  });
  return { controller, calls };
}

function interactiveHarness(workflowResult = {
  handled: true,
  ok: true,
  review_id: REVIEW_ID,
  reply: "Roster activated",
}) {
  const calls = { workflow: [], responses: [] };
  const controller = createInboundController({
    config: {
      groupId: GROUP,
      allowedSenderIds: [OWNER],
      botUsername: "RosterDemoBot",
    },
    extractTranscription: async () => {
      throw new Error("interactive callbacks must not extract");
    },
    runWorkflow: async (command, payload) => {
      calls.workflow.push({ command, payload });
      return typeof workflowResult === "function"
        ? workflowResult(command, payload)
        : workflowResult;
    },
  });
  const callbackContext = (overrides = {}) => {
    const respond = {
      reply: async (payload) => calls.responses.push({ method: "reply", payload }),
      editMessage: async (payload) => calls.responses.push({ method: "editMessage", payload }),
      editButtons: async (payload) => calls.responses.push({ method: "editButtons", payload }),
      clearButtons: async () => calls.responses.push({ method: "clearButtons" }),
    };
    return {
      channel: "telegram",
      isGroup: true,
      senderId: OWNER,
      auth: { isAuthorizedSender: true },
      callback: {
        chatId: GROUP,
        messageId: 42,
        messageText: "ROSTER",
        payload: `approve:${REVIEW_ID}`,
      },
      respond,
      ...overrides,
    };
  };
  return { controller, calls, callbackContext };
}

describe("wife-roster inbound controller", () => {
  it("rejects an unauthorized sender without invoking extraction or workflow", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({ SenderId: "test-intruder" }));
    assert.equal(result, "Not authorized.");
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("ignores another group", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({ ChatId: "test-other-group" }));
    assert.equal(result, null);
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("does not trigger run wife-roster without an explicit mention", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      ExplicitlyMentionedBot: false,
      WasMentioned: false,
      MentionSource: "none",
    }));
    assert.equal(result, null);
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("handles an explicitly mentioned ping without a model or workflow", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({ CommandBody: "ping", MediaPaths: [], MediaTypes: [] }));
    assert.equal(result, "pong");
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("handles the privacy-safe bot-addressed ping without a model or workflow", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/wife_roster_ping",
      RawBody: "/wife_roster_ping@RosterDemoBot",
      ExplicitlyMentionedBot: false,
      WasMentioned: true,
      MentionSource: "command_bypass",
      MediaPaths: [],
      MediaTypes: [],
    }));
    assert.equal(result, "pong");
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("handles the normalized privacy-safe roster caption", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/run_wife_roster",
      RawBody: "/run_wife_roster@RosterDemoBot",
      ExplicitlyMentionedBot: true,
      MentionSource: "explicit_bot",
    }));
    assert.equal(result, "review complete");
    assert.equal(calls.extraction.length, 1);
    assert.equal(calls.workflow.length, 1);
    assert.equal(calls.workflow[0].command, "review");
  });

  it("canonicalizes the free-text roster caption to the same workflow as the slash command", async () => {
    const slash = harness();
    const slashResult = await slash.controller.handle(context({
      CommandBody: "/run_wife_roster",
      RawBody: "/run_wife_roster@RosterDemoBot",
      ExplicitlyMentionedBot: true,
      MentionSource: "explicit_bot",
    }));

    const freeText = harness();
    const freeTextResult = await freeText.controller.handle(context({
      CommandBody: undefined,
      RawBody: "Run wife roster @RosterDemoBot",
      ExplicitlyMentionedBot: true,
      MentionSource: "explicit_bot",
    }));

    assert.equal(freeTextResult, slashResult);
    assert.equal(freeText.calls.workflow.length, 1);
    assert.equal(freeText.calls.workflow[0].command, "review");
    assert.deepEqual(freeText.calls.workflow[0].command, slash.calls.workflow[0].command);
  });

  it("adds exactly Approve and Revise buttons bound to the exact review ID", async () => {
    const calls = { extraction: 0, workflow: 0 };
    const controller = createInboundController({
      config: { groupId: GROUP, allowedSenderIds: [OWNER], botUsername: "RosterDemoBot" },
      extractTranscription: async () => {
        calls.extraction += 1;
        return { schema_version: 1, report_header: {}, rows: [] };
      },
      runWorkflow: async () => {
        calls.workflow += 1;
        return {
          handled: true,
          ok: true,
          reply: "ROSTER\n\nNeeds review: none",
          review_id: REVIEW_ID,
          can_approve: true,
        };
      },
    });

    const result = await controller.handle(context());

    assert.deepEqual(result, {
      text: "ROSTER\n\nNeeds review: none",
      channelData: { telegram: { buttons: [[
        {
          text: "Approve",
          callback_data: `wife-roster:approve:${REVIEW_ID}`,
        },
        {
          text: "Revise",
          callback_data: `wife-roster:revise:${REVIEW_ID}`,
        },
      ]] } },
    });
    assert.deepEqual(calls, { extraction: 1, workflow: 1 });
  });

  it("shows only Revise when the reviewed candidate has issues", async () => {
    const controller = createInboundController({
      config: { groupId: GROUP, allowedSenderIds: [OWNER], botUsername: "RosterDemoBot" },
      extractTranscription: async () => ({ schema_version: 1, report_header: {}, rows: [] }),
      runWorkflow: async () => ({
        handled: true,
        ok: true,
        reply: "NEEDS REVIEW",
        review_id: REVIEW_ID,
        can_approve: false,
      }),
    });

    const result = await controller.handle(context());

    assert.deepEqual(result.channelData.telegram.buttons, [[{
      text: "Revise",
      callback_data: `wife-roster:revise:${REVIEW_ID}`,
    }]]);
  });

  it("does not emit actionable buttons for an invalid review identity", async () => {
    const controller = createInboundController({
      config: { groupId: GROUP, allowedSenderIds: [OWNER], botUsername: "RosterDemoBot" },
      extractTranscription: async () => ({ schema_version: 1, report_header: {}, rows: [] }),
      runWorkflow: async () => ({
        handled: true,
        ok: true,
        reply: "NEEDS REVIEW",
        review_id: "candidate data must not enter callbacks",
        can_approve: true,
      }),
    });

    assert.equal(await controller.handle(context()), "NEEDS REVIEW");
  });

  it("does not emit buttons for a failed review response", async () => {
    const controller = createInboundController({
      config: { groupId: GROUP, allowedSenderIds: [OWNER], botUsername: "RosterDemoBot" },
      extractTranscription: async () => ({ schema_version: 1, report_header: {}, rows: [] }),
      runWorkflow: async () => ({
        handled: true,
        ok: false,
        reply: "Roster could not be reviewed safely.",
        review_id: REVIEW_ID,
        can_approve: true,
      }),
    });

    assert.equal(
      await controller.handle(context()),
      "Roster could not be reviewed safely.",
    );
  });

  it("uses the raw targeted caption as a defensive explicit-mention fallback", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/run_wife_roster",
      RawBody: "/run_wife_roster@RosterDemoBot",
      ExplicitlyMentionedBot: false,
      WasMentioned: true,
      MentionSource: "command_bypass",
    }));
    assert.equal(result, "review complete");
    assert.equal(calls.extraction.length, 1);
    assert.equal(calls.workflow.length, 1);
  });

  it("does not accept an unaddressed slash command as a mention", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/run_wife_roster",
      RawBody: "/run_wife_roster",
      ExplicitlyMentionedBot: false,
      WasMentioned: true,
      MentionSource: "command_bypass",
    }));
    assert.equal(result, null);
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("does not accept a slash command addressed to another bot", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/run_wife_roster@OtherBot",
      RawBody: "/run_wife_roster@OtherBot",
      ExplicitlyMentionedBot: false,
      WasMentioned: false,
      MentionSource: "none",
    }));
    assert.equal(result, null);
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("asks for an attachment without invoking extraction", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({ MediaPaths: [], MediaTypes: [] }));
    assert.equal(result, "Please attach the roster screenshot or PDF.");
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  for (const [path, type] of [
    ["/private/roster.jpg", "image/jpeg"],
    ["/private/roster.JPEG", "image/jpeg"],
    ["/private/roster.png", "image/png"],
    ["/private/roster.pdf", "application/pdf"],
  ]) {
    it(`accepts ${path.split(".").pop().toUpperCase()}`, async () => {
      const { controller, calls } = harness();
      const result = await controller.handle(context({ MediaPaths: [path], MediaTypes: [type] }));
      assert.equal(result, "review complete");
      assert.equal(calls.extraction.length, 1);
      assert.equal(calls.workflow.length, 1);
      assert.equal(calls.workflow[0].command, "review");
    });
  }

  it("treats several images as one candidate and preserves their order", async () => {
    const { controller, calls } = harness();
    const paths = ["/private/1.jpg", "/private/2.png", "/private/3.jpeg"];
    const types = ["image/jpeg", "image/png", "image/jpeg"];
    await controller.handle(context({ MediaPaths: paths, MediaTypes: types }));
    assert.equal(calls.extraction.length, 1);
    assert.deepEqual(calls.extraction[0].map((item) => item.path), paths);
    assert.equal(calls.workflow.length, 1);
    assert.deepEqual(calls.workflow[0].payload.attachments.map((item) => item.path), paths);
  });

  it("approves only through the deterministic workflow and never extracts again", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "APPROVE ROSTER",
      ExplicitlyMentionedBot: false,
      WasMentioned: true,
      MentionSource: "implicit_thread",
      MediaPaths: [],
      MediaTypes: [],
    }));
    assert.equal(result, "approve complete");
    assert.deepEqual(calls.extraction, []);
    assert.equal(calls.workflow.length, 1);
    assert.equal(calls.workflow[0].command, "approve");
  });

  it("supports a privacy-safe targeted approval command without extracting again", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({
      CommandBody: "/approve_roster",
      RawBody: "/approve_roster@RosterDemoBot",
      ExplicitlyMentionedBot: true,
      MediaPaths: [],
      MediaTypes: [],
    }));
    assert.equal(result, "approve complete");
    assert.deepEqual(calls.extraction, []);
    assert.equal(calls.workflow.length, 1);
    assert.equal(calls.workflow[0].command, "approve");
  });

  it("does not claim unrelated mentioned conversation", async () => {
    const { controller, calls } = harness();
    const result = await controller.handle(context({ CommandBody: "what time is it?" }));
    assert.equal(result, null);
    assert.deepEqual(calls, { extraction: [], workflow: [] });
  });

  it("approves an exact review deterministically and removes its buttons", async () => {
    const { controller, calls, callbackContext } = interactiveHarness();

    const result = await controller.handleInteractive(callbackContext());

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls.workflow, [{
      command: "approve",
      payload: { review_id: REVIEW_ID, group_id: GROUP, sender_id: OWNER },
    }]);
    assert.deepEqual(calls.responses, [
      { method: "clearButtons" },
      { method: "editMessage", payload: { text: "Roster activated" } },
    ]);
    assert.equal("submitText" in result, false);
  });

  it("revises an exact review deterministically and removes its buttons", async () => {
    const { controller, calls, callbackContext } = interactiveHarness({
      handled: true,
      ok: true,
      review_id: REVIEW_ID,
      reply: "Roster not activated.",
    });

    await controller.handleInteractive(callbackContext({
      callback: {
        chatId: GROUP,
        messageId: 42,
        messageText: "ROSTER",
        payload: `revise:${REVIEW_ID}`,
      },
    }));

    assert.deepEqual(calls.workflow, [{
      command: "revise",
      payload: { review_id: REVIEW_ID, group_id: GROUP, sender_id: OWNER },
    }]);
    assert.deepEqual(calls.responses, [
      { method: "clearButtons" },
      { method: "editMessage", payload: { text: "Roster not activated." } },
    ]);
  });

  it("rejects a successful workflow response bound to any other review ID", async () => {
    const { controller, calls, callbackContext } = interactiveHarness({
      handled: true,
      ok: true,
      review_id: "QrStUvWxYz012345",
      reply: "Roster activated",
    });

    const result = await controller.handleInteractive(callbackContext());

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls.responses, [
      { method: "clearButtons" },
      {
        method: "reply",
        payload: { text: "This roster review is no longer active." },
      },
    ]);
  });

  it("rejects an unauthorized callback without clearing the owner's buttons", async () => {
    const { controller, calls, callbackContext } = interactiveHarness();

    const result = await controller.handleInteractive(callbackContext({
      senderId: "test-intruder",
    }));

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls.workflow, []);
    assert.deepEqual(calls.responses, [{
      method: "reply",
      payload: { text: "Not authorized for roster changes." },
    }]);
  });

  it("allows a reviewer to ingest but not perform state-changing callbacks", async () => {
    const calls = { extraction: [], workflow: [], responses: [] };
    const controller = createInboundController({
      config: {
        groupId: GROUP,
        allowedSenderIds: [OWNER, REVIEWER],
        stateChangeSenderIds: [OWNER],
        botUsername: "RosterDemoBot",
      },
      extractTranscription: async (attachments) => {
        calls.extraction.push(attachments);
        return { schema_version: 1, report_header: {}, rows: [] };
      },
      runWorkflow: async (command, payload) => {
        calls.workflow.push({ command, payload });
        return { handled: true, ok: true, reply: `${command} complete` };
      },
    });

    assert.equal(
      await controller.handle(context({ SenderId: REVIEWER })),
      "review complete",
    );
    assert.equal(calls.extraction.length, 1);
    assert.equal(calls.workflow.length, 1);

    const result = await controller.handleInteractive({
      channel: "telegram",
      isGroup: true,
      senderId: REVIEWER,
      auth: { isAuthorizedSender: true },
      callback: {
        chatId: GROUP,
        messageId: 42,
        messageText: "ROSTER",
        payload: `approve:${REVIEW_ID}`,
      },
      respond: {
        reply: async (payload) => calls.responses.push(payload),
        clearButtons: async () => calls.responses.push("cleared"),
        editMessage: async (payload) => calls.responses.push(payload),
      },
    });

    assert.deepEqual(result, { handled: true });
    assert.equal(calls.workflow.length, 1);
    assert.deepEqual(calls.responses, [{
      text: "Not authorized for roster changes.",
    }]);
  });

  it("does not let an ingest-only reviewer use the slash approval command", async () => {
    let called = false;
    const controller = createInboundController({
      config: {
        groupId: GROUP,
        allowedSenderIds: [OWNER, REVIEWER],
        stateChangeSenderIds: [OWNER],
        botUsername: "RosterDemoBot",
      },
      extractTranscription: async () => ({}),
      runWorkflow: async () => {
        called = true;
        return { reply: "wrong" };
      },
    });
    const reply = await controller.handle(context({
      SenderId: REVIEWER,
      RawBody: "/approve_roster@RosterDemoBot",
      CommandBody: "/approve_roster",
      ExplicitlyMentionedBot: true,
    }));
    assert.equal(reply, "Not authorized for roster changes.");
    assert.equal(called, false);
  });

  it("requires OpenClaw's callback authorization fact in addition to the plugin allowlist", async () => {
    const { controller, calls, callbackContext } = interactiveHarness();

    await controller.handleInteractive(callbackContext({
      auth: { isAuthorizedSender: false },
    }));

    assert.deepEqual(calls.workflow, []);
    assert.deepEqual(calls.responses, [{
      method: "reply",
      payload: { text: "Not authorized for roster changes." },
    }]);
  });

  it("claims but silently rejects a callback from any other group", async () => {
    const { controller, calls, callbackContext } = interactiveHarness();

    const result = await controller.handleInteractive(callbackContext({
      callback: {
        chatId: "test-other-group",
        messageId: 42,
        messageText: "ROSTER",
        payload: `approve:${REVIEW_ID}`,
      },
    }));

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls, { workflow: [], responses: [] });
  });

  it("fails closed on malformed namespace payloads without invoking a model or workflow", async () => {
    const { controller, calls, callbackContext } = interactiveHarness();

    const result = await controller.handleInteractive(callbackContext({
      callback: {
        chatId: GROUP,
        messageId: 42,
        messageText: "ROSTER",
        payload: "approve:roster-data-or-an-invalid-id",
      },
    }));

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(calls.workflow, []);
    assert.deepEqual(calls.responses, [{
      method: "reply",
      payload: { text: "This roster review is no longer active." },
    }]);
  });

  for (const [label, reply] of [
    ["expired", "This roster review has expired. Please run the roster again."],
    ["superseded", "This roster review is no longer active."],
  ]) {
    it(`retires buttons and reports a ${label} review safely`, async () => {
      const { controller, calls, callbackContext } = interactiveHarness({
        handled: true,
        ok: false,
        terminal: true,
        error_code: label,
        reply,
      });

      await controller.handleInteractive(callbackContext());

      assert.deepEqual(calls.responses, [
        { method: "clearButtons" },
        { method: "reply", payload: { text: reply } },
      ]);
    });
  }

  it("leaves controls available after a non-terminal deterministic refusal", async () => {
    const { controller, calls, callbackContext } = interactiveHarness({
      handled: true,
      ok: false,
      terminal: false,
      error_code: "not_approvable",
      reply: "Roster has unresolved NEEDS REVIEW items.",
    });

    await controller.handleInteractive(callbackContext());

    assert.deepEqual(calls.responses, [{
      method: "reply",
      payload: { text: "Roster has unresolved NEEDS REVIEW items." },
    }]);
  });
});
