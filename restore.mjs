import { createReadStream, createWriteStream } from 'node:fs';
import { readFile, writeFile, open, link, rm, mkdir, stat } from 'node:fs/promises';
import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const magic = Buffer.from('NOTIONHUB-PRESERVATION-AES256GCM-V1\n');

export async function encryptFile(input, output, keyFile) {
  const key = await readFile(keyFile);
  if (key.length !== 32) throw new Error('The key file must contain exactly 32 bytes.');
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(magic);
  await writeFile(output, Buffer.concat([magic, iv]), { mode: 0o600 });
  await pipeline(createReadStream(input), cipher, createWriteStream(output, { flags: 'a', mode: 0o600 }));
  const handle = await open(output, 'a');
  try { await handle.write(cipher.getAuthTag()); } finally { await handle.close(); }
}

export async function decryptFile(input, output, keyFile) {
  const key = await readFile(keyFile);
  if (key.length !== 32) throw new Error('The key file must contain exactly 32 bytes.');
  const { size } = await stat(input);
  const headerSize = magic.length + 12;
  if (size < headerSize + 16) throw new Error('Encrypted archive is truncated.');
  const handle = await open(input, 'r');
  const header = Buffer.alloc(headerSize);
  const tag = Buffer.alloc(16);
  try {
    await handle.read(header, 0, header.length, 0);
    await handle.read(tag, 0, tag.length, size - 16);
  } finally { await handle.close(); }
  if (!header.subarray(0, magic.length).equals(magic)) throw new Error('Unknown archive format.');
  const decipher = createDecipheriv('aes-256-gcm', key, header.subarray(magic.length));
  decipher.setAAD(magic);
  decipher.setAuthTag(tag);
  const temporary = `${output}.partial-${randomBytes(8).toString('hex')}`;
  try {
    await pipeline(
      size === headerSize + 16 ? Readable.from([]) : createReadStream(input, { start: headerSize, end: size - 17 }),
      decipher,
      createWriteStream(temporary, { flags: 'wx', mode: 0o600 })
    );
    // Publishing with an exclusive hard link never overwrites an existing file.
    await link(temporary, output);
    await rm(temporary);
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
}

export async function hashFile(filename) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(filename)) digest.update(chunk);
  return digest.digest('hex');
}

export async function restoreDirectory(directory, keyFile, outputDirectory) {
  const manifest = JSON.parse(await readFile(path.join(directory, 'manifest.json'), 'utf8'));
  await mkdir(outputDirectory, { recursive: true, mode: 0o700 });
  for (const archive of manifest.archives) {
    const name = path.basename(archive.filename);
    if (name !== archive.filename) throw new Error('Archive names must be plain file names.');
    if (path.basename(archive.plaintextFilename) !== archive.plaintextFilename) throw new Error('Invalid output name.');
    let input = path.join(directory, name);
    let assembled;
    try {
      if (archive.parts?.length) {
        assembled = path.join(outputDirectory, `.ciphertext-parts-${randomBytes(8).toString('hex')}`);
        async function* parts() {
          for (const part of archive.parts) {
            if (path.basename(part.filename) !== part.filename) throw new Error('Invalid part name.');
            const filename = path.join(directory, part.filename);
            if (await hashFile(filename) !== part.sha256) throw new Error(`Part checksum mismatch: ${part.filename}`);
            for await (const chunk of createReadStream(filename)) yield chunk;
          }
        }
        await pipeline(Readable.from(parts()), createWriteStream(assembled, { flags: 'wx', mode: 0o600 }));
        input = assembled;
      }
      if (await hashFile(input) !== archive.encryptedSha256) throw new Error(`Encrypted checksum mismatch: ${name}`);
      const output = path.join(outputDirectory, archive.plaintextFilename);
      await decryptFile(input, output, keyFile);
      if (await hashFile(output) !== archive.plaintextSha256) {
        await rm(output, { force: true });
        throw new Error(`Restored checksum mismatch: ${name}`);
      }
    } finally {
      if (assembled) await rm(assembled, { force: true });
    }
    process.stdout.write(`Restored and verified ${archive.plaintextFilename}\n`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, first, second, third] = process.argv.slice(2);
  try {
    if (command === 'encrypt' && first && second && third) await encryptFile(first, second, third);
    else if (command === 'decrypt' && first && second && third) await decryptFile(first, second, third);
    else if (command === 'restore' && first && second && third) await restoreDirectory(first, second, third);
    else throw new Error('Usage: node restore.mjs encrypt|decrypt INPUT OUTPUT KEYFILE\n       node restore.mjs restore ARCHIVE_DIRECTORY KEYFILE OUTPUT_DIRECTORY');
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
