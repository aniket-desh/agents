#!/usr/bin/env node
/**
 * Protected stdio <-> hosted RunPod MCP transport.
 *
 * Enrollment is a separate owner action using pinned mcp-remote-client. This
 * runtime reads the cached bearer once and deliberately supplies NO authProvider:
 * a missing, expired, or revoked credential cannot open a browser, register a
 * client, redeem a code, rewrite the cache, or retry an uncertain paid mutation.
 * Install @modelcontextprotocol/sdk@1.32.2 beside this file under /opt/research-mcp.
 */
import { constants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ENDPOINT = 'https://mcp.runpod.io/';
export const SDK_VERSION = '1.32.2';
const MAX_TOKEN_FILE_BYTES = 64 * 1024;

export function validateEndpoint(value) {
  // A fixed origin also prevents credential forwarding through configuration
  // changes. The SDK's default redirect policy refuses cross-origin redirects.
  if (value !== ENDPOINT) throw new Error('Unsupported OAuth MCP endpoint');
  return new URL(value);
}

export async function readCachedAccessToken(filename, endpoint, now = Date.now()) {
  validateEndpoint(endpoint);
  if (!path.isAbsolute(filename)) throw new Error('OAuth cache path must be absolute');
  const expected = createHash('md5').update(endpoint).digest('hex') + '_tokens.json';
  if (path.basename(filename) !== expected || path.basename(path.dirname(filename)) !== 'mcp-remote-v1') {
    throw new Error('OAuth cache path does not match the pinned enrollment layout');
  }
  // No symlink in the path and no writable ancestor owned by an untrusted user.
  // These checks must run on local POSIX storage, never the permissive FUSE volume.
  const uid = process.geteuid();
  let current = path.parse(filename).root;
  const pieces = filename.slice(current.length).split(path.sep);
  for (let index = 0; index < pieces.length; index++) {
    current = path.join(current, pieces[index]);
    const info = await lstat(current);
    if (info.isSymbolicLink() || (info.uid !== 0 && info.uid !== uid) || (info.mode & 0o022)) {
      throw new Error('OAuth cache path is not protected local POSIX storage');
    }
    if (index === pieces.length - 2 && (!info.isDirectory() || (info.mode & 0o077))) {
      throw new Error('OAuth token directory must be private');
    }
  }
  const descriptor = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await descriptor.stat();
    if (!info.isFile() || (info.uid !== 0 && info.uid !== uid) || (info.mode & 0o077) || info.size > MAX_TOKEN_FILE_BYTES) {
      throw new Error('OAuth token file must be a private, bounded regular file');
    }
    let tokens;
    try {
      tokens = JSON.parse(await descriptor.readFile({ encoding: 'utf8' }));
    } catch {
      // JSON parse diagnostics can echo input; never expose them through stderr.
      throw new Error('OAuth token cache is invalid; owner enrollment is required');
    }
    if (!tokens || typeof tokens !== 'object' || Array.isArray(tokens) ||
        typeof tokens.access_token !== 'string' || !tokens.access_token ||
        tokens.access_token.length > 16384 || /[\s\x00-\x1f\x7f]/.test(tokens.access_token) ||
        typeof tokens.token_type !== 'string' || tokens.token_type.toLowerCase() !== 'bearer') {
      throw new Error('OAuth token cache lacks a valid bearer; owner enrollment is required');
    }
    if (tokens.issuer !== undefined && tokens.issuer !== 'https://mcp.runpod.io' && tokens.issuer !== ENDPOINT) {
      throw new Error('OAuth token issuer does not match RunPod');
    }
    if (tokens.expires_at !== undefined &&
        (typeof tokens.expires_at !== 'number' || !Number.isFinite(tokens.expires_at) || tokens.expires_at <= now + 60000)) {
      throw new Error('OAuth bearer is expired or near expiry; owner enrollment is required');
    }
    // Current RunPod responses contain no expiry or refresh token. If that
    // changes, fail closed at expiry rather than implementing refresh here.
    return tokens.access_token;
  } finally {
    await descriptor.close();
  }
}

export function safeFailure(error) {
  const code = error && typeof error.code === 'number' ? error.code : undefined;
  if (code === 401 || code === 403) {
    return 'RunPod OAuth access was refused; owner enrollment or network review is required';
  }
  return 'RunPod OAuth transport failed; inspect protected service diagnostics';
}

/** Wire transports without a Client/Server object consuming initialize itself. */
export async function serve(local, remote, { input = process.stdin, log = (line) => process.stderr.write(line + '\n') } = {}) {
  let closing = false;
  let resolveDone;
  const done = new Promise((resolve) => { resolveDone = resolve; });
  const initializations = new Set();
  const close = async (code = 0) => {
    if (closing) return;
    closing = true;
    input.off('end', onEnd);
    process.off('SIGINT', onSignal);
    process.off('SIGTERM', onSignal);
    await Promise.allSettled([remote.close(), local.close()]);
    resolveDone(code);
  };
  const fail = (error) => {
    if (closing) return;
    log(safeFailure(error));
    void close(1);
  };
  const onEnd = () => { void close(0); };
  const onSignal = () => { void close(0); };
  local.onmessage = (message) => {
    if (closing) return;
    if (message.method === 'initialize' && message.id !== undefined) initializations.add(message.id);
    Promise.resolve().then(() => remote.send(message)).catch(fail);
  };
  remote.onmessage = (message) => {
    if (closing) return;
    if (initializations.delete(message.id) && typeof message.result?.protocolVersion === 'string') {
      remote.setProtocolVersion(message.result.protocolVersion);
    }
    Promise.resolve().then(() => local.send(message)).catch(fail);
  };
  local.onerror = fail;
  remote.onerror = fail;
  local.onclose = onEnd;
  remote.onclose = onEnd;
  input.once('end', onEnd);
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);
  try {
    await remote.start();
    await local.start();
  } catch (error) {
    fail(error);
  }
  return done;
}

export async function main(args = process.argv.slice(2)) {
  if (args.length !== 2) {
    process.stderr.write('Usage: oauth-proxy.mjs https://mcp.runpod.io/ ABSOLUTE_OAUTH_TOKEN_CACHE\n');
    return 2;
  }
  try {
    const endpoint = validateEndpoint(args[0]);
    const token = await readCachedAccessToken(args[1], args[0]);
    const [{ StreamableHTTPClientTransport }, { StdioServerTransport }] = await Promise.all([
      import('@modelcontextprotocol/sdk/client/streamableHttp.js'),
      import('@modelcontextprotocol/sdk/server/stdio.js'),
    ]);
    const remote = new StreamableHTTPClientTransport(endpoint, {
      requestInit: { headers: { Authorization: 'Bearer ' + token } },
      // No authProvider and no redirectPolicy:'follow': runtime never enrolls.
      reconnectionOptions: { maxRetries: 0 },
    });
    return await serve(new StdioServerTransport(), remote);
  } catch {
    // Cache, SDK, and HTTP errors may contain credentials or response bodies.
    process.stderr.write('RunPod OAuth bridge unavailable; owner enrollment or protected runtime repair is required\n');
    return 1;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  process.exit(await main());
}
