import { mkdir, copyFile } from 'node:fs/promises';
await mkdir('app/static/vendor', { recursive: true });
await copyFile('node_modules/hls.js/dist/hls.min.js', 'app/static/vendor/hls.min.js');
await copyFile('node_modules/hls.js/LICENSE', 'app/static/vendor/HLS-LICENSE.txt');
