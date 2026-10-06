// Ordered configuration blocks (Dell OS6 + Dell OS10): editor model, the read-only CLI
// preview, and early STRUCTURAL feedback that mirrors the API's integrity checks
// (apps/network-api/app/config_blocks.py). There is no command policy here or in the
// backend: any CLI may be submitted and the device is the syntax authority. The API and
// worker re-validate everything.

import type { ConfigBlock } from "../api/types";

export const MAX_BLOCKS = 50;
export const MAX_COMMANDS_PER_BLOCK = 100;
export const MAX_TOTAL_COMMANDS = 500;
export const MAX_COMMAND_LENGTH = 1000;
export const MAX_PARENT_LENGTH = 1000;
export const MAX_VERIFICATION_COMMANDS = 20;

const PRINTABLE = /^[\x20-\x7e]+$/;
const MODE_COMMAND = /^(configure|config|conf\s+t|end|exit|do)(\s|$)/i;
const READ_ONLY = /^show\s+\S/i;

export interface EditorBlock {
  id: string;
  parent: string;
  commands: string[];
}

let nextId = 0;
export function newBlockId(): string {
  nextId += 1;
  return `block-${Date.now().toString(36)}-${nextId}`;
}

export function emptyBlock(): EditorBlock {
  return { id: newBlockId(), parent: "", commands: [""] };
}

/** Editor state -> request blocks: trims ends of lines, drops empty command rows. Order kept. */
export function toRequestBlocks(blocks: EditorBlock[]): ConfigBlock[] {
  return blocks.map((block) => ({
    parent: block.parent.trim() ? block.parent.trim() : null,
    commands: block.commands.map((command) => command.trim()).filter((command) => command.length > 0),
  }));
}

/** Read-only CLI preview in exact execution order (same format as the worker/API). */
export function renderCliPreview(blocks: ConfigBlock[]): string {
  return blocks
    .map((block, index) => {
      const header = `! Block ${index + 1}${block.parent ? "" : " - Global"}`;
      const body = block.parent ? [block.parent, ...block.commands.map((c) => ` ${c}`)] : block.commands;
      return [header, ...body].join("\n");
    })
    .join("\n\n");
}

function lineProblem(text: string, where: string, maxLength: number): string | null {
  if (text.length > maxLength) return `${where} is longer than ${maxLength} characters.`;
  if (!PRINTABLE.test(text)) return `${where} must be single-line printable text (no control characters).`;
  if (MODE_COMMAND.test(text)) return `${where} ("${text}"): configure / end / exit / do are handled by the platform.`;
  return null;
}

/** Structural problems only (empty blocks, limits, control characters, mode commands). */
export function blockProblems(blocks: ConfigBlock[]): string[] {
  const problems: string[] = [];
  if (blocks.length === 0) problems.push("Add at least one configuration block.");
  if (blocks.length > MAX_BLOCKS) problems.push(`At most ${MAX_BLOCKS} blocks are allowed.`);
  let total = 0;
  blocks.forEach((block, index) => {
    const label = `Block ${index + 1}`;
    if (block.parent) {
      const problem = lineProblem(block.parent, `${label} parent`, MAX_PARENT_LENGTH);
      if (problem) problems.push(problem);
    }
    if (block.commands.length === 0) problems.push(`${label} must contain at least one command.`);
    if (block.commands.length > MAX_COMMANDS_PER_BLOCK) {
      problems.push(`${label} has more than ${MAX_COMMANDS_PER_BLOCK} commands.`);
    }
    total += block.commands.length;
    block.commands.forEach((command, n) => {
      const problem = lineProblem(command, `${label} command ${n + 1}`, MAX_COMMAND_LENGTH);
      if (problem) problems.push(problem);
    });
  });
  if (total > MAX_TOTAL_COMMANDS) problems.push(`At most ${MAX_TOTAL_COMMANDS} commands are allowed in one change.`);
  return problems;
}

export function verificationProblems(commands: string[]): string[] {
  const problems: string[] = [];
  if (commands.length > MAX_VERIFICATION_COMMANDS) {
    problems.push(`At most ${MAX_VERIFICATION_COMMANDS} verification commands are allowed.`);
  }
  commands.forEach((command, n) => {
    if (!PRINTABLE.test(command) || command.length > MAX_COMMAND_LENGTH) {
      problems.push(`Verification command ${n + 1} must be single-line printable text.`);
    } else if (!READ_ONLY.test(command)) {
      problems.push(`Verification command ${n + 1} ("${command}") must be a read-only "show ..." command.`);
    }
  });
  return problems;
}

export function commandTotal(blocks: ConfigBlock[]): number {
  return blocks.reduce((sum, block) => sum + block.commands.length, 0);
}

/** Pasting several lines into one command field splits them into separate commands. */
export function splitPastedLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+$/, ""))
    .filter((line) => line.trim().length > 0);
}
