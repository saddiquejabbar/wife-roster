import { lstat, readFile } from "node:fs/promises";
import path from "node:path";


const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
const MAX_TOTAL_BYTES = 40 * 1024 * 1024;
const MAX_PDF_PAGES = 20;
const MAX_PDF_RENDER_PAGES = 10;
const MAX_PDF_PIXELS = 4_000_000;


export class TranscriptionExtractionError extends Error {
  constructor(message = "Roster transcription failed") {
    super(message);
    this.name = "TranscriptionExtractionError";
  }
}


export function createTranscriptionExtractor(api, config) {
  return async (attachments) => {
    const prompt = await readFile(config.promptPath, "utf8");
    const prepared = [];
    let totalBytes = 0;
    for (let index = 0; index < attachments.length; index += 1) {
      const attachment = attachments[index];
      const file = await readSafeAttachment(attachment);
      totalBytes += file.buffer.length;
      if (totalBytes > MAX_TOTAL_BYTES) {
        throw new TranscriptionExtractionError("Combined roster attachments are too large");
      }
      if (file.extension === ".pdf") {
        prepared.push(...await preparePdfInputs(file.buffer, index));
      } else {
        prepared.push({
          type: "text",
          text: `The next image is roster source_index ${index}. Preserve that source_index in every row transcribed from it.`,
        });
        prepared.push({
          type: "image",
          buffer: file.buffer,
          fileName: `source-${index + 1}${file.extension}`,
          mime: file.mime,
        });
      }
    }
    if (!prepared.some((value) => value.type === "image")) {
      throw new TranscriptionExtractionError();
    }
    const { provider, model } = resolveExtractionModel(config.extractionModel);
    let result;
    try {
      result = await api.runtime.mediaUnderstanding.extractStructuredWithModel({
        provider,
        model,
        input: prepared,
        instructions: prompt,
        schemaName: "wife-roster.transcription.v1",
        jsonSchema: TRANSCRIPTION_SCHEMA,
        jsonMode: true,
        cfg: api.config,
        timeoutMs: config.extractionTimeoutMs ?? 300000,
      });
    } catch {
      throw new TranscriptionExtractionError();
    }
    const value = result?.parsed ?? parseStructuredText(result?.text);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new TranscriptionExtractionError();
    }
    return value;
  };
}


async function readSafeAttachment(attachment) {
  if (typeof attachment.path !== "string" || !path.isAbsolute(attachment.path)) {
    throw new TranscriptionExtractionError("Roster attachment path is invalid");
  }
  const info = await lstat(attachment.path);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 1 || info.size > MAX_ATTACHMENT_BYTES) {
    throw new TranscriptionExtractionError("Roster attachment size is invalid");
  }
  const buffer = await readFile(attachment.path);
  if (buffer.length !== info.size) {
    throw new TranscriptionExtractionError("Roster attachment changed while being read");
  }
  const extension = path.extname(attachment.path).toLowerCase();
  const mime = normalizedMime(attachment.contentType);
  if (!matchesType(buffer, extension, mime)) {
    throw new TranscriptionExtractionError("Roster attachment type is invalid");
  }
  return { buffer, extension, mime };
}


function normalizedMime(value) {
  return String(value ?? "").split(";", 1)[0].trim().toLowerCase();
}


function matchesType(buffer, extension, mime) {
  if (extension === ".png" && (!mime || mime === "image/png")) {
    return buffer.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"));
  }
  if ((extension === ".jpg" || extension === ".jpeg") && (!mime || mime === "image/jpeg")) {
    return buffer.subarray(0, 3).equals(Buffer.from("ffd8ff", "hex"));
  }
  if (extension === ".pdf" && (!mime || mime === "application/pdf")) {
    return buffer.subarray(0, 5).toString("ascii") === "%PDF-";
  }
  return false;
}


async function preparePdfInputs(buffer, sourceIndex) {
  let engine;
  let document;
  try {
    ({ createEngine: engine } = await import("clawpdf"));
    document = await (await engine()).open(new Uint8Array(buffer));
    const maxPages = Math.min(document.pageCount, MAX_PDF_PAGES);
    const text = (await document.extract({
      mode: "text",
      maxPages,
      maxTextChars: 200000,
    })).text ?? "";
    const renderCount = text.trim().length >= 200
      ? 1
      : Math.min(maxPages, MAX_PDF_RENDER_PAGES);
    const pages = Array.from({ length: renderCount }, (_, index) => index + 1);
    const rendered = await document.extract({
      mode: "images",
      pages,
      image: {
        maxDimension: 10000,
        maxPixels: MAX_PDF_PIXELS,
        forms: true,
      },
    });
    const inputs = [{
      type: "text",
      text: [
        `PDF roster source_index ${sourceIndex}. Preserve that source_index in every row from this PDF.`,
        text ? `Extracted PDF text layer:\n${text}` : "The PDF text layer was missing or unusable; use the rendered pages.",
      ].join("\n\n").slice(0, 210000),
    }];
    for (let index = 0; index < rendered.images.length; index += 1) {
      const image = rendered.images[index];
      inputs.push({
        type: "image",
        buffer: Buffer.from(image.bytes),
        fileName: `source-${sourceIndex + 1}-page-${index + 1}.png`,
        mime: image.mimeType || "image/png",
      });
    }
    if (!inputs.some((value) => value.type === "image")) {
      throw new TranscriptionExtractionError("PDF could not be rendered safely");
    }
    return inputs;
  } catch (error) {
    if (error instanceof TranscriptionExtractionError) throw error;
    throw new TranscriptionExtractionError("PDF could not be read safely");
  } finally {
    document?.destroy();
  }
}


function resolveExtractionModel(reference) {
  if (typeof reference !== "string" || !reference.includes("/")) {
    throw new TranscriptionExtractionError("No image-capable extraction model is configured");
  }
  const separator = reference.indexOf("/");
  const provider = reference.slice(0, separator).trim();
  const model = reference.slice(separator + 1).trim();
  if (!provider || !model) {
    throw new TranscriptionExtractionError("No image-capable extraction model is configured");
  }
  return { provider, model };
}


function parseStructuredText(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw new TranscriptionExtractionError();
  }
  try {
    return JSON.parse(value);
  } catch {
    throw new TranscriptionExtractionError();
  }
}


const NULLABLE_STRING = { type: ["string", "null"] };
const TRANSCRIPTION_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["schema_version", "coverage", "report_header", "rows"],
  properties: {
    schema_version: { const: 1 },
    coverage: { enum: ["FULL", "PARTIAL", "UNCERTAIN"] },
    report_header: {
      type: "object",
      additionalProperties: false,
      required: ["period_from", "period_to", "port_local_notice_present"],
      properties: {
        period_from: NULLABLE_STRING,
        period_to: NULLABLE_STRING,
        port_local_notice_present: { type: "boolean" },
      },
    },
    rows: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: [
          "source_index", "row_index", "start_date", "day", "flight_number",
          "sector", "duty", "rpt", "std", "sta", "flight_time", "remarks", "unreadable",
        ],
        properties: {
          source_index: { type: "integer", minimum: 0 },
          row_index: { type: "integer", minimum: 0 },
          start_date: NULLABLE_STRING,
          day: NULLABLE_STRING,
          flight_number: NULLABLE_STRING,
          sector: NULLABLE_STRING,
          duty: NULLABLE_STRING,
          rpt: NULLABLE_STRING,
          std: NULLABLE_STRING,
          sta: NULLABLE_STRING,
          flight_time: NULLABLE_STRING,
          remarks: NULLABLE_STRING,
          unreadable: { type: "array", items: { type: "string" } },
        },
      },
    },
  },
};
