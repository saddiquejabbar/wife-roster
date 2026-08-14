import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { registerWifeRosterInteractiveHandler } from "../interactive.js";


describe("wife-roster OpenClaw interactive registration", () => {
  it("registers the exact Telegram namespace and directly invokes the controller", async () => {
    const registrations = [];
    const controllerCalls = [];
    const api = {
      logger: {},
      registerInteractiveHandler(registration) {
        registrations.push(registration);
      },
    };
    const controller = {
      async handleInteractive(ctx) {
        controllerCalls.push(ctx);
        return { handled: true };
      },
    };
    registerWifeRosterInteractiveHandler(api, controller);

    assert.equal(registrations.length, 1);
    assert.equal(registrations[0].channel, "telegram");
    assert.equal(registrations[0].namespace, "wife-roster");
    const callbackContext = { callback: { payload: "approve:AbCdEfGhIjKlMnOp" } };
    const result = await registrations[0].handler(callbackContext);
    assert.deepEqual(result, { handled: true });
    assert.deepEqual(controllerCalls, [callbackContext]);
    assert.equal("submitText" in result, false);
  });

  it("contains unexpected callback failures without submitting text to an agent", async () => {
    const registrations = [];
    const warnings = [];
    const replies = [];
    const api = {
      logger: { warn: (value) => warnings.push(value) },
      registerInteractiveHandler(registration) {
        registrations.push(registration);
      },
    };
    registerWifeRosterInteractiveHandler(api, {
      async handleInteractive() {
        throw new Error("private failure detail");
      },
    });

    const result = await registrations[0].handler({
      respond: { reply: async (payload) => replies.push(payload) },
    });

    assert.deepEqual(result, { handled: true });
    assert.deepEqual(replies, [{
      text: "wife-roster could not complete this request safely.",
    }]);
    assert.deepEqual(warnings, ["wife-roster interactive request failed safely"]);
    assert.equal("submitText" in result, false);
  });
});
