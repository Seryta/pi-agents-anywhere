FROM node:22-bookworm-slim AS verify
WORKDIR /plugin
COPY package.json ./
# Tests and the plugin must resolve the exact same SDK Session class.
RUN npm install --ignore-scripts --no-package-lock --no-save \
    @deepseek-ai/dsh-session@0.2.1-rc.2 @deepseek-ai/dsh-llm@0.2.1-rc.2
COPY lib ./lib
COPY test ./test
COPY cordis.patch.yml dsh.plugin.json README.md LICENSE ./
RUN mkdir /out \
    && node --test test/*.test.mjs > /out/tests.tap \
    && node --check lib/client.js \
    && npm pack --ignore-scripts --pack-destination /out \
    && mkdir /packaged && tar -xzf /out/dsh-btw-*.tgz -C /packaged \
    && ln -s /plugin/node_modules /packaged/package/node_modules \
    && cp -r test /packaged/package/test \
    && node --test /packaged/package/test/*.test.mjs > /out/packaged-tests.tap \
    && node --check /packaged/package/lib/client.js
RUN node --input-type=module <<'JS'
import { readFileSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
const pkg = JSON.parse(readFileSync('package.json'))
const sdk = JSON.parse(readFileSync('node_modules/@deepseek-ai/dsh-session/package.json'))
const artifact = `${pkg.name}-${pkg.version}.tgz`
const sha256 = createHash('sha256').update(readFileSync(`/out/${artifact}`)).digest('hex')
writeFileSync('/out/verification.json', JSON.stringify({ok:true, name:pkg.name, version:pkg.version,
  node:process.version, sdkVersion:sdk.version, artifact, sha256,
  scope:'Host semantics on source and packed plugin; browser bundle syntax. No real model or browser UI.'}, null, 2)+'\n')
JS

FROM scratch AS artifacts
COPY --from=verify /out/ /
