import { useEffect, useMemo, useState } from "react";
import { Search, RefreshCw } from "lucide-react";
import { PageShell, PageHeader } from "@/components/app/PageShell";
import config from "@/config";
import { fetchAuthenticatedJson } from "@/api";

const getRows = (payload) => {
  if (Array.isArray(payload)) return payload;
  return [
    payload?.results,
    payload?.data?.results,
    payload?.items,
    payload?.data,
    payload?.rows,
  ].find(Array.isArray) || [];
};

const resolveNextUrl = (next, currentUrl) => {
  if (!next) return "";
  try {
    return new URL(next, currentUrl).toString();
  } catch {
    return String(next);
  }
};

async function fetchEngineerStoreRows() {
  const base = String(config.baseURL || "").replace(/\/+$/, "");
  if (!base) throw new Error("The API base URL is not configured.");

  let url = `${base}/inventory/inventory/engineer-store/?paginate=1&page_size=100`;
  const visited = new Set();
  const allRows = [];

  while (url && !visited.has(url)) {
    visited.add(url);
    const payload = await fetchAuthenticatedJson(url, { cache: "no-store" });
    allRows.push(...getRows(payload));
    url = resolveNextUrl(payload?.next || payload?.data?.next, url);
  }

  return allRows;
}

export default function EngineerInventoryPage() {
  const [rows, setRows] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");

    fetchEngineerStoreRows()
      .then((data) => {
        if (active) setRows(data);
      })
      .catch((requestError) => {
        if (active) {
          setRows([]);
          setError(requestError?.message || "Unable to load In-Store inventory.");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [refreshKey]);

  const visibleRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return rows;
    return rows.filter((row) =>
      [row.component_id, row.specification, row.category, row.component_type, row.uom]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }, [rows, search]);

  return (
    <PageShell>
      <PageHeader
        title="In-Store Inventory"
        subtitle="View component IDs, specifications, and current In-Store quantities."
      />

      <section className="space-y-4" aria-label="In-Store inventory">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <label className="relative block w-full sm:max-w-xl">
            <Search className="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search component ID or specification..."
              aria-label="Search In-Store components"
              className="w-full rounded-2xl border border-border bg-card py-3 pl-11 pr-4 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/10"
            />
          </label>
          <button
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
            className="inline-flex h-11 items-center justify-center gap-2 rounded-xl border border-border bg-card px-4 text-sm font-medium text-foreground transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>

        <div className="rounded-2xl border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
          {loading
            ? "Loading In-Store components..."
            : `${visibleRows.length} component stock row${visibleRows.length === 1 ? "" : "s"}`}
        </div>

        {error && (
          <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[650px] text-left text-sm">
              <thead className="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th scope="col" className="px-4 py-4 text-center">S.No</th>
                  <th scope="col" className="px-4 py-4">Component ID</th>
                  <th scope="col" className="px-4 py-4">Specification</th>
                  <th scope="col" className="px-4 py-4 text-right">Qty</th>
                  <th scope="col" className="px-4 py-4">UOM</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {loading ? (
                  <tr><td colSpan={5} className="px-4 py-12 text-center text-muted-foreground">Loading...</td></tr>
                ) : visibleRows.length ? (
                  visibleRows.map((row, index) => (
                    <tr key={row.id ?? `${row.component_id}-${index}`} className="hover:bg-muted/20">
                      <td className="px-4 py-4 text-center text-muted-foreground">{index + 1}</td>
                      <td className="px-4 py-4 font-medium text-foreground">{row.component_id || "N/A"}</td>
                      <td className="px-4 py-4 text-foreground">{row.specification || "—"}</td>
                      <td className="px-4 py-4 text-right font-semibold tabular-nums text-foreground">{row.quantity ?? 0}</td>
                      <td className="px-4 py-4 text-muted-foreground">{row.uom || "—"}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-muted-foreground">
                      {error ? "Inventory could not be loaded." : search ? "No components match your search." : "No In-Store stock is currently available."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </PageShell>
  );
}
