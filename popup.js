const cardContainer = document.getElementById("card-container");
const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const apiKeyInput = document.getElementById("hackclub-api-key");
const apiKeyBox = document.getElementById("hackclub-api-key-box");
const statusMessage = document.getElementById("status-message");
const runOutput = document.getElementById("run-output");

const PROVIDER = {
    storageKey: "hackclubApiKey",
    label: "Hack Club",
    endpoint: "https://ai.hackclub.com/proxy/v1/chat/completions",
    model: "openai/gpt-oss-20b"
};

const MAPPED_SITES_STORAGE_KEY = "hackclubMappedSites";
const MAX_DISCOVERED_LINKS = 120;
const MAX_CARDS = 50;

const responseSchema = {
    type: "object",
    properties: {
        run: {
            type: "object",
            additionalProperties: false,
            properties: {
                needed: { type: "boolean" },
                javascript: { type: "string" },
                message: { type: "string" }
            },
            required: ["needed", "javascript", "message"]
        },
        cards: {
            type: "array",
            items: {
                type: "object",
                additionalProperties: false,
                properties: {
                    title: { type: "string" },
                    description: { type: "string" },
                    icon: { type: "string" },
                    url: { type: "string" },
                    priority: { type: "integer", minimum: 1, maximum: 100 }
                },
                required: ["title", "description", "icon", "url", "priority"]
            }
        }
    },
    required: ["run", "cards"]
};

// State for in-flight request cancellation and per-tab context caching
let activeSearchController = null;
let cachedPageContext = null;

function setStatus(message, isError = false) {
    if (!statusMessage) return;
    statusMessage.textContent = message;
    statusMessage.dataset.error = String(isError);
}

function isHttpUrl(value) {
    try {
        const url = new URL(value);
        return url.protocol === "http:" || url.protocol === "https:";
    } catch {
        return false;
    }
}

function normalizeHttpUrl(value) {
    try {
        const url = new URL(value);
        if (url.protocol !== "http:" && url.protocol !== "https:") {
            return "";
        }
        url.hash = "";
        if (url.pathname.length > 1) {
            url.pathname = url.pathname.replace(/\/+$/, "");
        }
        return url.href;
    } catch {
        return "";
    }
}

function addCard(card) {
    if (!card || typeof card.title !== "string" || !card.title.trim()) {
        throw new TypeError("Each card needs a non-empty title.");
    }

    const cardElement = document.createElement("article");
    cardElement.className = "card";

    if (card.icon) {
        const icon = document.createElement("span");
        icon.className = "card-icon";
        icon.textContent = card.icon.trim();
        cardElement.append(icon);
    }

    const title = document.createElement("h2");
    title.className = "card-title";
    title.textContent = card.title.trim();
    cardElement.append(title);

    const priority = Number.isFinite(card.priority)
        ? Math.max(1, Math.min(100, Math.round(card.priority)))
        : 50;
    const priorityLabel = document.createElement("span");
    priorityLabel.className = "card-priority";
    priorityLabel.textContent = `Priority ${priority}/100`;
    cardElement.append(priorityLabel);

    if (card.description) {
        const description = document.createElement("p");
        description.className = "card-description";
        description.textContent = card.description.trim();
        cardElement.append(description);
    }

    const validUrl = normalizeHttpUrl(card.url);
    if (validUrl) {
        const link = document.createElement("a");
        link.className = "card-link";
        link.href = validUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Open";
        link.setAttribute("aria-label", `Open ${card.title.trim()}`);
        cardElement.append(link);

        // Allow clicking anywhere on the card without creating duplicate keyboard tab stops
        cardElement.addEventListener("click", (event) => {
            if (event.target.closest?.("a") || window.getSelection()?.toString()) {
                return;
            }
            link.click();
        });
    }

    cardContainer.append(cardElement);
    return cardElement;
}

function addCards(cards) {
    if (!Array.isArray(cards)) {
        throw new TypeError("The cards value must be an array.");
    }

    const rendered = [];
    for (const card of cards) {
        try {
            rendered.push(addCard(card));
        } catch {
            // Skip malformed individual cards so one bad item doesn't break the entire list
        }
    }
    return rendered;
}

function clearCards() {
    cardContainer.replaceChildren();
}

function cardFromDiscoveredUrl(url) {
    const parsedUrl = new URL(url);
    const path = parsedUrl.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
    let title = path.at(-1) || parsedUrl.hostname;
    try {
        title = decodeURIComponent(title);
    } catch {
        // Keep the encoded path segment as a readable fallback
    }
    title = title.replace(/[-_]+/g, " ").trim() || parsedUrl.hostname;

    return {
        title: title.replace(/\b\w/g, (char) => char.toUpperCase()),
        description: `Discovered from ${parsedUrl.hostname}.`,
        icon: "🔗",
        url: parsedUrl.href,
        priority: 50
    };
}

function sortAndDedupeCards(cards) {
    const seenUrls = new Set();
    return cards
        .filter((card) => card && typeof card.title === "string" && card.title.trim())
        .map((card) => ({
            ...card,
            url: normalizeHttpUrl(card.url),
            priority: Number.isFinite(card.priority)
                ? Math.max(1, Math.min(100, Math.round(card.priority)))
                : 50
        }))
        .filter((card) => {
            if (!card.url || seenUrls.has(card.url)) {
                return false;
            }
            seenUrls.add(card.url);
            return true;
        })
        .sort((left, right) => right.priority - left.priority)
        .slice(0, MAX_CARDS);
}

function ensureMinimumCards(result, discoveredLinks) {
    const rawCards = Array.isArray(result.cards) ? result.cards : [];
    const cards = sortAndDedupeCards(rawCards);
    const knownUrls = new Set(cards.map((card) => card.url));

    for (const rawUrl of discoveredLinks) {
        const url = normalizeHttpUrl(rawUrl);
        if (!url || knownUrls.has(url) || cards.length >= MAX_CARDS) {
            continue;
        }
        cards.push(cardFromDiscoveredUrl(url));
        knownUrls.add(url);
    }

    return sortAndDedupeCards(cards);
}

async function readStoredApiKey(storageKey) {
    if (globalThis.chrome?.storage?.local) {
        const result = await chrome.storage.local.get(storageKey);
        return result[storageKey] || "";
    }
    return localStorage.getItem(storageKey) || "";
}

async function storeApiKey(storageKey, apiKey) {
    if (globalThis.chrome?.storage?.local) {
        await chrome.storage.local.set({ [storageKey]: apiKey });
        return;
    }
    localStorage.setItem(storageKey, apiKey);
}

async function readMappedSites() {
    if (globalThis.chrome?.storage?.local) {
        const result = await chrome.storage.local.get(MAPPED_SITES_STORAGE_KEY);
        return result[MAPPED_SITES_STORAGE_KEY] || {};
    }
    try {
        return JSON.parse(localStorage.getItem(MAPPED_SITES_STORAGE_KEY) || "{}");
    } catch {
        return {};
    }
}

async function storeMappedSite(siteKey, data) {
    const mappedSites = await readMappedSites();
    mappedSites[siteKey] = data;
    if (globalThis.chrome?.storage?.local) {
        await chrome.storage.local.set({ [MAPPED_SITES_STORAGE_KEY]: mappedSites });
    } else {
        localStorage.setItem(MAPPED_SITES_STORAGE_KEY, JSON.stringify(mappedSites));
    }
}

async function getApiKey(provider) {
    // Prioritize current input value in case the user just typed a new key and hit Enter
    const inputKey = apiKeyInput?.value?.trim() || "";
    if (inputKey) {
        await storeApiKey(provider.storageKey, inputKey);
        return inputKey;
    }
    return readStoredApiKey(provider.storageKey);
}

async function getPageContext() {
    if (!globalThis.chrome?.tabs?.query || !globalThis.chrome?.scripting?.executeScript) {
        return { text: "No browser extension page context is available.", discoveredLinks: [] };
    }

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
        return { text: "No active page context is available.", discoveredLinks: [] };
    }

    // Reuse cached context if the user is running follow-up searches on the exact same tab URL
    if (cachedPageContext && cachedPageContext.tabId === tab.id && cachedPageContext.url === tab.url) {
        return cachedPageContext.data;
    }

    // Avoid calling executeScript on restricted browser pages (chrome://, edge://, about:, etc.)
    if (!isHttpUrl(tab.url)) {
        return {
            text: `Active page: ${tab.title || "Browser page"} (${tab.url || "restricted URL"})`,
            discoveredLinks: []
        };
    }

    const siteKey = new URL(tab.url).origin;
    const mappedSites = await readMappedSites();
    if (mappedSites[siteKey]?.data) {
        cachedPageContext = { tabId: tab.id, url: tab.url, data: mappedSites[siteKey].data };
        return mappedSites[siteKey].data;
    }

    try {
        const [{ result }] = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: async () => {
                const discoveredLinks = new Set(
                    [...document.querySelectorAll("a[href]")]
                        .map((anchor) => {
                            try {
                                const url = new URL(anchor.href, location.href);
                                if (url.protocol !== "http:" && url.protocol !== "https:") {
                                    return "";
                                }
                                url.hash = ""; // Strip hashes to deduplicate same-page anchors
                                return url.href;
                            } catch {
                                return "";
                            }
                        })
                        .filter(Boolean)
                );

                const sitemapUrls = new Set([new URL("/sitemap.xml", location.origin).href]);
                const request = async (url) => {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 900);
                    try {
                        const response = await fetch(url, {
                            credentials: "same-origin",
                            signal: controller.signal
                        });
                        return response.ok ? await response.text() : "";
                    } catch {
                        return "";
                    } finally {
                        clearTimeout(timeout);
                    }
                };

                const getSitemapLocations = (text) =>
                    [...text.matchAll(/<loc[^>]*>\s*([^<]+?)\s*<\/loc>/gi)].map((match) => match[1].trim());

                const addSitemapEntries = (text) => {
                    for (const location of getSitemapLocations(text)) {
                        try {
                            const url = new URL(location, globalThis.location.href);
                            if (url.origin === globalThis.location.origin) {
                                discoveredLinks.add(url.href);
                            }
                        } catch {
                            // Ignore malformed sitemap entries
                        }
                    }
                };

                const robots = await request(new URL("/robots.txt", location.origin).href);
                for (const match of robots.matchAll(/^\s*sitemap:\s*(\S+)/gim)) {
                    try {
                        const url = new URL(match[1], location.href);
                        if (url.origin === location.origin) {
                            sitemapUrls.add(url.href);
                        }
                    } catch {
                        // Ignore malformed sitemap declarations
                    }
                }

                const sitemapTexts = await Promise.all([...sitemapUrls].slice(0, 3).map(request));
                for (const text of sitemapTexts) {
                    if (/<sitemapindex[\s>]/i.test(text)) {
                        const childSitemaps = getSitemapLocations(text)
                            .map((value) => {
                                try {
                                    const url = new URL(value, globalThis.location.href);
                                    return url.origin === globalThis.location.origin ? url.href : "";
                                } catch {
                                    return "";
                                }
                            })
                            .filter(Boolean)
                            .slice(0, 3);
                        const childTexts = await Promise.all(childSitemaps.map(request));
                        childTexts.forEach(addSitemapEntries);
                    } else {
                        addSitemapEntries(text);
                    }
                }

                return {
                    title: document.title,
                    url: location.href,
                    selection: window.getSelection()?.toString().trim().slice(0, 2000) || "",
                    text: document.body?.innerText?.replace(/\s+/g, " ").trim().slice(0, 8000) || "",
                    discoveredLinks: [...discoveredLinks].slice(0, 120)
                };
            }
        });

        const data = {
            text: JSON.stringify(result),
            discoveredLinks: result?.discoveredLinks || []
        };
        cachedPageContext = { tabId: tab.id, url: tab.url, data };
        await storeMappedSite(siteKey, { mappedAt: Date.now(), data });
        return data;
    } catch {
        return {
            text: `Active page: ${tab.title || "Unknown page"} (${tab.url || "unknown URL"})`,
            discoveredLinks: []
        };
    }
}

function buildPrompt(query, pageContext) {
    return `You recommend important links for the user.

User search: ${query || "No search submitted; summarize the links from the current page."}

Current page context and verified links discovered from the active page, robots.txt, and sitemap files:
${pageContext}

Return only the requested JSON structure. The run section must describe whether extra JavaScript is needed.
If no JavaScript is needed, set needed to false and javascript to an empty string.
If JavaScript is needed, provide a small function body as text, but never include destructive actions,
credential access, eval, Function constructors, or arbitrary downloads. Cards must contain useful, verified
http or https URLs when links are available. Return as many distinct useful verified URLs as possible,
up to ${MAX_CARDS}, not just the most important few. Every card must include a priority from 1 to 100,
where 100 is most needed.`;
}

async function requestProvider(provider, query, pageContext, signal) {
    const apiKey = await getApiKey(provider);
    if (!apiKey) {
        throw new Error(`${provider.label} API key is not configured.`);
    }

    const requestBody = {
        model: provider.model,
        messages: [
            {
                role: "system",
                content: "Return only valid JSON matching the requested schema."
            },
            {
                role: "user",
                content: buildPrompt(query, pageContext)
            }
        ],
        response_format: {
            type: "json_schema",
            json_schema: {
                name: "link_recommendations",
                strict: true,
                schema: responseSchema
            }
        },
        temperature: 0.2,
        max_tokens: 5000
    };

    let response;
    try {
        response = await fetch(provider.endpoint, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${apiKey}`
            },
            body: JSON.stringify(requestBody),
            signal
        });
    } catch (error) {
        if (error.name === "AbortError") {
            throw error;
        }
        if (error instanceof TypeError) {
            throw new Error(
                "Network request blocked. Reload the extension after updating manifest.json, " +
                "then verify that ai.hackclub.com is reachable."
            );
        }
        throw new Error(`${provider.label} network request failed: ${error.message}`);
    }

    let payload;
    try {
        payload = await response.json();
    } catch {
        throw new Error(`${provider.label} returned a non-JSON response (${response.status}).`);
    }

    if (!response.ok) {
        throw new Error(payload.error?.message || `${provider.label} request failed (${response.status}).`);
    }

    const text = payload.choices?.[0]?.message?.content;
    if (!text) {
        throw new Error(`${provider.label} returned an empty response.`);
    }

    try {
        return typeof text === "string" ? JSON.parse(text) : text;
    } catch {
        throw new Error(`${provider.label} returned invalid structured data.`);
    }
}

async function requestWithFallback(query, signal) {
    if (!globalThis.chrome?.runtime?.sendMessage || !globalThis.chrome?.tabs?.query) {
        throw new Error("The extension background worker is unavailable. Reload the extension and try again.");
    }

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !isHttpUrl(tab.url)) {
        throw new Error("Open an http or https page before searching.");
    }

    const initial = await chrome.runtime.sendMessage({
        type: "start-search",
        tabId: tab.id,
        tabUrl: tab.url,
        query
    });
    if (initial?.error) {
        throw new Error(initial.error);
    }

    const jobKey = initial.jobKey;
    let job = initial.job;
    while (job?.status === "running") {
        if (signal.aborted) {
            throw new DOMException("Search superseded.", "AbortError");
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
        const update = await chrome.runtime.sendMessage({ type: "get-search-job", jobKey });
        if (update?.error) {
            throw new Error(update.error);
        }
        job = update?.job;
    }

    if (!job) {
        throw new Error("The background search job was lost. Try searching again.");
    }
    if (job.status === "error") {
        throw new Error(job.message || "The background search failed.");
    }
    return { provider: PROVIDER, result: job.result, discoveredLinks: [] };
}

function displayResponse(result, discoveredLinks) {
    if (!result || !result.run || !Array.isArray(result.cards)) {
        throw new Error("The response did not contain run and cards sections.");
    }

    result.cards = ensureMinimumCards(result, discoveredLinks);
    clearCards();
    addCards(result.cards);
    runOutput.textContent = result.run.needed
        ? result.run.javascript || result.run.message
        : result.run.message || "No additional JavaScript is needed.";
}

async function runSearch(query = "") {
    // Cancel any previous in-flight search so an earlier slow request never overwrites a newer search
    activeSearchController?.abort();
    const controller = new AbortController();
    activeSearchController = controller;

    setStatus("Finding relevant links...");
    showLoadingCards();
    runOutput.textContent = "";

    try {
        const { provider, result, discoveredLinks } = await requestWithFallback(query, controller.signal);
        if (controller.signal.aborted) return;

        displayResponse(result, discoveredLinks);
        if (apiKeyBox) {
            apiKeyBox.hidden = true;
        }
        setStatus(`${result.cards.length} link${result.cards.length === 1 ? "" : "s"} found with ${provider.label}.`);
    } catch (error) {
        if (error.name === "AbortError") {
            return;
        }
        clearCards();
        if (apiKeyBox) {
            apiKeyBox.hidden = false;
        }
        setStatus(error.message, true);
    } finally {
        if (activeSearchController === controller) {
            activeSearchController = null;
        }
    }
}

function showLoadingCards() {
    cardContainer.replaceChildren();
    const fragment = document.createDocumentFragment();

    for (let index = 0; index < 3; index += 1) {
        const loadingCard = document.createElement("article");
        loadingCard.className = "card card-loading";
        loadingCard.setAttribute("aria-hidden", "true");
        for (const className of [
            "loading-line loading-icon",
            "loading-line loading-title",
            "loading-line loading-description",
            "loading-line loading-description short"
        ]) {
            const line = document.createElement("span");
            line.className = className;
            loadingCard.append(line);
        }
        fragment.append(loadingCard);
    }

    cardContainer.append(fragment);
}

searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runSearch(searchInput.value.trim());
});

apiKeyInput?.addEventListener("change", async () => {
    const apiKey = apiKeyInput.value.trim();
    if (!apiKey) {
        return;
    }

    try {
        await storeApiKey(PROVIDER.storageKey, apiKey);
        setStatus(`${PROVIDER.label} API key saved locally. Running search...`);
        await runSearch(searchInput.value.trim());
    } catch (error) {
        setStatus(`Could not save API key: ${error.message}`, true);
    }
});

window.addCard = addCard;
window.addCards = addCards;
window.clearCards = clearCards;
window.runSearch = runSearch;

(async () => {
    try {
        const storedKey = await readStoredApiKey(PROVIDER.storageKey);
        if (apiKeyInput) {
            apiKeyInput.value = storedKey;
        }

        if (!storedKey) {
            setStatus(`Enter your ${PROVIDER.label} API key to get started.`);
            return;
        }

        await runSearch("");
    } catch (error) {
        setStatus(error.message, true);
    }
})();