import path from "node:path";

import { INTERACTIVE_NAMESPACE } from "./interactive.js";


const SUPPORTED = new Map([
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".png", "image/png"],
  [".pdf", "application/pdf"],
]);
const REVIEW_ID = /^[A-Za-z0-9_-]{16}$/;


export function createInboundController({ config, extractTranscription, runWorkflow }) {
  const allowedSenders = new Set(config.allowedSenderIds.map((value) => String(value)));
  const stateChangeSenders = new Set(
    (config.stateChangeSenderIds ?? config.allowedSenderIds).map((value) => String(value)),
  );
  const groupId = String(config.groupId);
  const expectedOrigin = `telegram:${groupId}`;

  return {
    async handle(ctx) {
      if (!isTargetGroup(ctx, groupId, expectedOrigin)) {
        return null;
      }
      const parsed = parseCommand(ctx, config.botUsername);
      const command = parsed.command;
      if (!isWorkflowCommand(command)) {
        return null;
      }
      if (ctx.GroupRequireMention !== true) {
        return null;
      }
      const explicitMention = ctx.ExplicitlyMentionedBot === true || parsed.addressedSlash;
      const implicitReply = ctx.WasMentioned === true && ctx.MentionSource === "implicit_thread";
      const commandMentioned = command === "approve roster"
        ? explicitMention || implicitReply
        : explicitMention;
      if (!commandMentioned) {
        return null;
      }
      if (!allowedSenders.has(String(ctx.SenderId ?? ""))) {
        return "Not authorized.";
      }
      if (
        command === "approve roster"
        && !stateChangeSenders.has(String(ctx.SenderId ?? ""))
      ) {
        return "Not authorized for roster changes.";
      }
      if (command === "ping") {
        return "pong";
      }
      if (command === "approve roster") {
        const result = await runWorkflow("approve", {
          group_id: groupId,
          sender_id: String(ctx.SenderId),
        });
        return result.reply;
      }

      const attachments = resolveAttachments(ctx);
      if (attachments.length === 0) {
        return "Please attach the roster screenshot or PDF.";
      }
      const unsupported = attachments.find((value) => !isSupported(value));
      if (unsupported) {
        return "Supported roster files are PDF, PNG, JPG, and JPEG.";
      }
      const transcription = await extractTranscription(attachments, ctx);
      const result = await runWorkflow("review", {
        group_id: groupId,
        sender_id: String(ctx.SenderId),
        mentioned: true,
        attachments: attachments.map((value) => ({
          path: value.path,
          filename: path.basename(value.path),
          content_type: value.contentType,
        })),
        transcription,
      });
      return reviewReplyPayload(result);
    },

    async handleInteractive(ctx) {
      // The namespace has already been claimed by OpenClaw before this method
      // runs. Always handle malformed or out-of-scope payloads locally so they
      // can never fall through to an agent/model turn.
      if (!isAuthorizedCallbackContext(ctx, groupId, stateChangeSenders)) {
        if (isTargetCallbackContext(ctx, groupId)) {
          await ctx.respond.reply({ text: "Not authorized for roster changes." });
        }
        return { handled: true };
      }
      const parsed = parseInteractivePayload(ctx.callback?.payload);
      if (parsed === null) {
        await ctx.respond.reply({ text: "This roster review is no longer active." });
        return { handled: true };
      }
      const result = await runWorkflow(parsed.action, {
        review_id: parsed.reviewId,
        group_id: groupId,
        sender_id: String(ctx.senderId),
      });
      if (result.ok === true && result.review_id !== parsed.reviewId) {
        await ctx.respond.clearButtons();
        await ctx.respond.reply({ text: "This roster review is no longer active." });
        return { handled: true };
      }
      if (result.ok === true) {
        // OpenClaw 2026.7.1 does not reliably clear existing markup when an
        // empty buttons array is passed to editMessage. Clear it explicitly,
        // then replace the same review message with the terminal result.
        await ctx.respond.clearButtons();
        await ctx.respond.editMessage({ text: result.reply });
        return { handled: true };
      }
      // Retire controls only when the deterministic workflow confirms that
      // this review is terminal (expired, superseded, already acted on, or
      // otherwise unusable). A non-terminal operational refusal remains safe
      // to revise or retry.
      if (result.terminal === true) {
        await ctx.respond.clearButtons();
      }
      await ctx.respond.reply({ text: result.reply });
      return { handled: true };
    },
  };
}


function reviewReplyPayload(result) {
  if (!result || typeof result !== "object" || typeof result.reply !== "string") {
    throw new TypeError("wife-roster review response is invalid");
  }
  const reviewId = typeof result.review_id === "string" ? result.review_id : "";
  if (result.ok !== true || !REVIEW_ID.test(reviewId)) {
    return result.reply;
  }
  const buttons = result.can_approve === true
    ? [[
      callbackButton("Approve", "approve", reviewId),
      callbackButton("Revise", "revise", reviewId),
    ]]
    : [[callbackButton("Revise", "revise", reviewId)]];
  return {
    text: result.reply,
    channelData: { telegram: { buttons } },
  };
}


function callbackButton(text, action, reviewId) {
  return {
    text,
    callback_data: `${INTERACTIVE_NAMESPACE}:${action}:${reviewId}`,
  };
}


function isTargetCallbackContext(ctx, groupId) {
  return ctx?.channel === "telegram"
    && ctx.isGroup === true
    && String(ctx.callback?.chatId ?? "") === groupId;
}


function isAuthorizedCallbackContext(ctx, groupId, allowedSenders) {
  return isTargetCallbackContext(ctx, groupId)
    && ctx.auth?.isAuthorizedSender === true
    && allowedSenders.has(String(ctx.senderId ?? ""));
}


function parseInteractivePayload(value) {
  const match = String(value ?? "").match(/^(approve|revise):([A-Za-z0-9_-]{16})$/);
  if (match === null) {
    return null;
  }
  return { action: match[1], reviewId: match[2] };
}


function isTargetGroup(ctx, groupId, expectedOrigin) {
  const channel = String(ctx.Provider ?? ctx.Surface ?? "").toLowerCase();
  if (channel !== "telegram" || ctx.ChatType !== "group") {
    return false;
  }
  if (String(ctx.ChatId ?? "") !== groupId) {
    return false;
  }
  const origin = String(ctx.OriginatingTo ?? "");
  return !origin || origin === expectedOrigin;
}


function parseCommand(ctx, botUsername) {
  const rawBody = String(ctx.RawBody ?? "").trim();
  const escaped = String(botUsername).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const addressed = rawBody.match(
    new RegExp(`^/(ping|wife_roster_ping|run_wife_roster|approve_roster)@${escaped}\\s*$`, "i"),
  );
  const source = ctx.CommandBody ?? ctx.BodyForCommands ?? rawBody;
  const normalized = normalizeCommand(source, botUsername);
  return {
    command: canonicalCommand(normalized),
    addressedSlash: addressed !== null,
  };
}


function normalizeCommand(value, botUsername) {
  const escaped = String(botUsername).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return String(value)
    .replace(new RegExp(`@${escaped}(?=$|\\s)`, "ig"), " ")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}


function canonicalCommand(value) {
  return {
    "/ping": "ping",
    "/wife_roster_ping": "ping",
    "/run_wife_roster": "run wife-roster",
    "/approve_roster": "approve roster",
    "run wife roster": "run wife-roster",
  }[value] ?? value;
}


function isWorkflowCommand(value) {
  return value === "ping" || value === "run wife-roster" || value === "approve roster";
}


function resolveAttachments(ctx) {
  const paths = Array.isArray(ctx.MediaPaths)
    ? ctx.MediaPaths
    : (ctx.MediaPath ? [ctx.MediaPath] : []);
  const types = Array.isArray(ctx.MediaTypes)
    ? ctx.MediaTypes
    : (ctx.MediaType ? [ctx.MediaType] : []);
  return paths
    .filter((value) => typeof value === "string" && value.length > 0)
    .map((value, index) => ({
      path: value,
      contentType: typeof types[index] === "string" ? types[index].split(";", 1)[0].trim().toLowerCase() : "",
    }));
}


function isSupported(attachment) {
  const extension = path.extname(attachment.path).toLowerCase();
  const expected = SUPPORTED.get(extension);
  return expected !== undefined && (!attachment.contentType || attachment.contentType === expected);
}
