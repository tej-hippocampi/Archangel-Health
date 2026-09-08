/* Hash only one bounded slice, off the UI thread. Never a whole-file prepass. */
self.onmessage = async ({data}) => {
  try {
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", await data.arrayBuffer()));
    self.postMessage({checksum: btoa(String.fromCharCode(...hash))});
  } catch (_) { self.postMessage({error: "Could not read this file."}); }
};
