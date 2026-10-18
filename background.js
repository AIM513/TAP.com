const PROVIDER = {
    storageKey: "hackclubApiKey",
    label: "Hack Club",
    endpoint: "https://ai.hackclub.com/proxy/v1/chat/completions",
    model: "openai/gpt-oss-20b"
};

const MAPPED_SITES_STORAGE_KEY = "hackclubMappedSites";
const SEARCH_JOBS_STORAGE_KEY = "hackclubSearchJobs";
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

function cardFromDiscoveredUrl(url) {
    const parsedUrl = new URL(url);
    const path = parsedUrl.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
    let title = path.at(-1) || parsedUrl.hostname;
    try {
        title = decodeURIComponent(title);
    } catch {
        // Keep the encoded path segment as a readable fallback.
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

function completeCards(result, discoveredLinks) {
    const cards = sortAndDedupeCards(result.cards);
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

async function getStorage(key) {
    const result = await chrome.storage.local.get(key);
    return result[key] || {};
}

async function setStorage(key, value) {
    await chrome.storage.local.set({ [key]: value });
}

async function mapSite(tabId, tabUrl) {
    const siteKey = new URL(tabUrl).origin;
    const mappedSites = await getStorage(MAPPED_SITES_STORAGE_KEY);
    if (mappedSites[siteKey]?.data) {
        return mappedSites[siteKey].data;
    }

    const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId },
        func: async () => {
            const discoveredLinks = new Set(
                [...document.querySelectorAll("a[href]")]
                    .map((anchor) => {
                        try {
                            const url = new URL(anchor.href, location.href);
                            if (url.protocol !== "http:" && url.protocol !== "https:") return "";
                            url.hash = "";
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
                    const response = await fetch(url, { credentials: "same-origin", signal: controller.signal });
                    return response.ok ? await response.text() : "";
                } catch {
                    return "";
                } finally {
                    clearTimeout(timeout);
                }
            };
            const locations = (text) =>
                [...text.matchAll(/<loc[^>]*>\s*([^<]+?)\s*<\/loc>/gi)].map((match) => match[1].trim());
            const addEntries = (text) => {
                for (const value of locations(text)) {
                    try {
                        const url = new URL(value, location.href);
                        if (url.origin === location.origin) discoveredLinks.add(url.href);
                    } catch {
                        // Ignore malformed sitemap entries.
                    }
                }
            };
            const robots = await request(new URL("/robots.txt", location.origin).href);
            for (const match of robots.matchAll(/^\s*sitemap:\s*(\S+)/gim)) {
                try {
                    const url = new URL(match[1], location.href);
                    if (url.origin === location.origin) sitemapUrls.add(url.href);
                } catch {
                    // Ignore malformed sitemap declarations.
                }
            }
            const sitemapTexts = await Promise.all([...sitemapUrls].slice(0, 3).map(request));
            for (const text of sitemapTexts) {
                if (/<sitemapindex[\s>]/i.test(text)) {
                    const children = locations(text).map((value) => {
                        try {
                            const url = new URL(value, location.href);
                            return url.origin === location.origin ? url.href : "";
                        } catch {
                            return "";
                        }
                    }).filter(Boolean).slice(0, 3);
                    (await Promise.all(children.map(request))).forEach(addEntries);
                } else {
                    addEntries(text);
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

    const data = { text: JSON.stringify(result), discoveredLinks: result?.discoveredLinks || [] };
    mappedSites[siteKey] = { mappedAt: Date.now(), data };
    await setStorage(MAPPED_SITES_STORAGE_KEY, mappedSites);
    return data;
}

function buildPrompt(query, pageContext) {
    return `You recommend important links for the user.

User search: ${query || "No search submitted; summarize the links from the current page."}

Current page context and verified links discovered from the active page, robots.txt, and sitemap files:
${pageContext}

Return only the requested JSON structure. Cards must contain useful, verified http or https URLs.
Return as many distinct useful verified URLs as possible, up to ${MAX_CARDS}, not just the most important few.
Every card must include a priority from 1 to 100, where 100 is most needed.`;
}

async function requestProvider(query, pageContext) {
    const stored = await chrome.storage.local.get(PROVIDER.storageKey);
    const apiKey = stored[PROVIDER.storageKey];
    if (!apiKey) throw new Error(`${PROVIDER.label} API key is not configured.`);

    const response = await fetch(PROVIDER.endpoint, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${apiKey}`
        },
        body: JSON.stringify({
            model: PROVIDER.model,
            messages: [
                { role: "system", content: "Return only valid JSON matching the requested schema." },
                { role: "user", content: buildPrompt(query, pageContext) }
            ],
            response_format: {
                type: "json_schema",
                json_schema: { name: "link_recommendations", strict: true, schema: responseSchema }
            },
            temperature: 0.2,
            max_tokens: 5000
        })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || `${PROVIDER.label} request failed (${response.status}).`);
    const text = payload.choices?.[0]?.message?.content;
    if (!text) throw new Error(`${PROVIDER.label} returned an empty response.`);
    return typeof text === "string" ? JSON.parse(text) : text;
}

async function runJob(jobKey, tabId, tabUrl, query) {
    const jobs = await getStorage(SEARCH_JOBS_STORAGE_KEY);
    try {
        const pageContext = await mapSite(tabId, tabUrl);
        const result = await requestProvider(query, pageContext.text);
        result.cards = completeCards(result, pageContext.discoveredLinks);
        jobs[jobKey] = {
            status: "complete",
            provider: PROVIDER.label,
            result,
            updatedAt: Date.now()
        };
    } catch (error) {
        jobs[jobKey] = { status: "error", message: error.message, updatedAt: Date.now() };
    }
    await setStorage(SEARCH_JOBS_STORAGE_KEY, jobs);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || !["start-search", "get-search-job"].includes(message.type)) return;
    (async () => {
        const jobs = await getStorage(SEARCH_JOBS_STORAGE_KEY);
        if (message.type === "get-search-job") {
            sendResponse({ job: jobs[message.jobKey] || null });
            return;
        }

        const jobKey = `${new URL(message.tabUrl).origin}|${message.query || ""}`;
        const existing = jobs[jobKey];
        if (existing?.status === "running" || existing?.status === "complete") {
            sendResponse({ jobKey, job: existing });
            return;
        }

        jobs[jobKey] = { status: "running", startedAt: Date.now(), updatedAt: Date.now() };
        await setStorage(SEARCH_JOBS_STORAGE_KEY, jobs);
        await runJob(jobKey, message.tabId, message.tabUrl, message.query || "");
        const completedJobs = await getStorage(SEARCH_JOBS_STORAGE_KEY);
        sendResponse({ jobKey, job: completedJobs[jobKey] });
    })().catch((error) => sendResponse({ error: error.message }));
    return true;
});
