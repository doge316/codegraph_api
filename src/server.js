import { createHttpApi } from './http-api.js';

const hostname = process.env.HOST || '127.0.0.1';
const port = Number(process.env.PORT || 3000);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('PORT must be an integer between 1 and 65535.');
}

const app = createHttpApi();
app.listen({ hostname, port }, (server) => {
    // The Node adapter passes the server here but does not set app.server.
    app.server = server;
});
await app.server.raw.serve();
console.log(`CodeGraph HTTP API listening on http://${hostname}:${port}`);

for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, async () => {
        try {
            await app.stop();
        } catch (error) {
            console.error(error);
            process.exitCode = 1;
        }
    });
}
