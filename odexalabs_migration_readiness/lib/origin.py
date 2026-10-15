# -*- coding: utf-8 -*-
"""INV-001: where an installed module came from, and what to do for the target.

A LABELLED HEURISTIC. Every classification carries the signals that decided
it, and reads "unclear" when the signals conflict or there are none; it is
never guessed into a confident label. Everything read here is already on the
instance: `ir_module_module` fields, the manifest and README on disk, the
module's path, and the addons checkout's `.git/config`. Nothing is fetched.
The links it produces are followed by the user's browser, not by this module.

`module_type` is deliberately NOT a signal: it is Odoo's App Store taxonomy
(official / industries), not provenance.
"""

import re

ODOO_STANDARD = "odoo_standard"
ODOO_ENTERPRISE = "odoo_enterprise"
OCA = "oca"
PAID_VENDOR = "paid_vendor"
THIRD_PARTY = "third_party"
STUDIO_IMPORTED = "studio_imported"
IN_HOUSE = "in_house"
UNCLEAR = "unclear"

BUCKETS = [
    (ODOO_STANDARD, "Odoo standard"),
    (ODOO_ENTERPRISE, "Odoo Enterprise"),
    (OCA, "OCA"),
    (PAID_VENDOR, "Paid App Store vendor"),
    (THIRD_PARTY, "Other third-party"),
    (STUDIO_IMPORTED, "Studio / imported"),
    (IN_HOUSE, "In-house"),
    (UNCLEAR, "Unclear"),
]
BUCKET_LABELS = dict(BUCKETS)

_OCA_AUTHOR = re.compile(r"odoo community association|\bOCA\b", re.IGNORECASE)
_ODOO_SA_AUTHOR = re.compile(r"^\s*(odoo s\.?a\.?|odoo sa|openerp s\.?a\.?)\s*$", re.IGNORECASE)
_OCA_REPO = re.compile(r"github\.com/OCA/([A-Za-z0-9_.-]+)", re.IGNORECASE)
_URL_DOMAIN = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]*@)?([^/:?#]+)", re.IGNORECASE)
_PROPRIETARY_LICENSES = {"OPL-1", "Other proprietary"}
_SCAFFOLD_AUTHORS = {"my company"}
_SCAFFOLD_WEBSITES = {"yourcompany.com", "www.yourcompany.com"}
_HOSTING_DOMAINS = {"github.com", "gitlab.com", "bitbucket.org", "odoo.com", "www.odoo.com", "apps.odoo.com"}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def domain_of(url_or_email):
    """The host part of a URL, or the domain of an e-mail address, lower-case."""
    value = (url_or_email or "").strip()
    if not value:
        return None
    if "@" in value and "://" not in value:
        return value.rsplit("@", 1)[1].strip().lower() or None
    match = _URL_DOMAIN.match(value)
    if not match:
        return None
    host = match.group(1).lower()
    return host or None


def strip_www(domain):
    """The domain without a leading "www." (str.removeprefix needs Python 3.9;
    Odoo 15 and 16 accept 3.7)."""
    return domain[4:] if domain.startswith("www.") else domain


def strip_credentials(url):
    """Remove any user:password@ from a remote URL before it is stored or shown."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^@/]*@", r"\1", url or "", flags=re.IGNORECASE)


def oca_repos(*texts):
    """OCA repository names mentioned in any of `texts`, in order, de-duplicated."""
    seen = []
    for text in texts:
        for repo in _OCA_REPO.findall(text or ""):
            repo = repo[:-4] if repo.lower().endswith(".git") else repo
            if repo and repo not in seen:
                seen.append(repo)
    return seen


def _is_company(author, companies):
    author_n = _norm(author)
    if not author_n:
        return False
    for name in companies:
        name_n = _norm(name)
        if not name_n:
            continue
        if author_n == name_n or (len(name_n) >= 4 and name_n in author_n):
            return True
    return False


def classify(info, companies=(), company_domains=(), current_series="", target_series=""):
    """Classify one module. `info` is a dict of what the instance holds about it.

    Keys read (all optional): name, author, website, url, license, description,
    readme, manifest (raw dict), git_remotes (list), imported (bool or None),
    to_buy, standard (bool), standard_license, has_store_packaging (bool),
    on_disk (bool).

    Returns a dict: bucket, label, signals (list of str), oca_repo, vendor_site,
    support, conflict (bool).
    """
    name = info.get("name") or ""
    author = (info.get("author") or "").strip()
    website = (info.get("website") or "").strip()
    manifest = info.get("manifest") or {}
    license_ = (info.get("license") or manifest.get("license") or "").strip()
    remotes = [strip_credentials(r) for r in info.get("git_remotes") or []]
    signals = []
    strong = set()
    company_domains = {d.lower() for d in company_domains if d}

    if not info.get("standard") and _ODOO_SA_AUTHOR.match(author):
        lic = license_ or info.get("standard_license") or ""
        signals.append("author: %s" % author)
        if lic == "OEEL-1":
            signals.append("licence OEEL-1 (Odoo Enterprise)")
        return _result(ODOO_ENTERPRISE if lic == "OEEL-1" else ODOO_STANDARD, signals, False)
    if info.get("standard"):
        lic = info.get("standard_license") or license_
        if info.get("standard_by_licence"):
            signals.append("licence OEEL-1 recorded for it in this database (Odoo Enterprise; "
                           "Enterprise module names are not in the vendored lists)")
        else:
            signals.append("listed in Odoo %s's own module list" % current_series)
        if lic == "OEEL-1":
            signals.append("licence OEEL-1 (Odoo Enterprise)")
            bucket = ODOO_ENTERPRISE
        else:
            bucket = ODOO_STANDARD
        return _result(bucket, signals, False)

    if name == "studio_customization":
        signals.append("module name studio_customization (Odoo Studio's export module)")
        strong.add(STUDIO_IMPORTED)
    if info.get("imported"):
        signals.append("imported as a data module (installed from an archive, no code path)")
        strong.add(STUDIO_IMPORTED)

    repos = oca_repos(website, info.get("url"), info.get("description"), info.get("readme"), *remotes)
    if _OCA_AUTHOR.search(author):
        signals.append("author names the OCA: %s" % author)
        strong.add(OCA)
    for label, text in (("website", website), ("git remote", " ".join(remotes))):
        if _OCA_REPO.search(text or ""):
            signals.append("%s is an OCA repository: %s" % (label, text))
            strong.add(OCA)
    if _OCA_REPO.search((info.get("description") or "") + "\n" + (info.get("readme") or "")):
        signals.append("README links an OCA repository (github.com/OCA/%s)" % (repos[0] if repos else "…"))
        strong.add(OCA)
    if manifest.get("development_status") or manifest.get("maintainers"):
        signals.append("manifest carries OCA-convention keys (development_status / maintainers)")

    price = manifest.get("price")
    try:
        price_value = float(price) if price not in (None, "", False) else 0.0
    except (TypeError, ValueError):
        price_value = 0.0
    if price_value > 0:
        signals.append("manifest price %s %s" % (price, manifest.get("currency") or ""))
        strong.add(PAID_VENDOR)
    if license_ in _PROPRIETARY_LICENSES:
        signals.append("licence %s (proprietary)" % license_)
        strong.add(PAID_VENDOR)
    if info.get("to_buy"):
        signals.append("flagged to_buy in the module list")
        strong.add(PAID_VENDOR)

    site_domain = domain_of(website)
    if author and _is_company(author, companies):
        signals.append("author matches this database's company: %s" % author)
        strong.add(IN_HOUSE)
    elif _norm(author) in _SCAFFOLD_AUTHORS:
        signals.append("author is odoo-bin scaffold's placeholder: %s" % author)
        strong.add(IN_HOUSE)
    if site_domain and (site_domain in company_domains or strip_www(site_domain) in company_domains):
        signals.append("website is on this database's company domain: %s" % site_domain)
        strong.add(IN_HOUSE)
    elif site_domain in _SCAFFOLD_WEBSITES:
        signals.append("website is odoo-bin scaffold's placeholder: %s" % website)
        strong.add(IN_HOUSE)

    vendor_site = None
    if (
        author
        and OCA not in strong
        and IN_HOUSE not in strong
        and not _ODOO_SA_AUTHOR.match(author)
    ):
        signals.append("named author: %s" % author)
        strong.add(THIRD_PARTY)
    if (
        site_domain
        and OCA not in strong
        and site_domain not in _HOSTING_DOMAINS
        and site_domain not in _SCAFFOLD_WEBSITES
        and site_domain not in company_domains
    ):
        vendor_site = website
        if THIRD_PARTY not in strong and PAID_VENDOR not in strong:
            signals.append("website on a vendor domain: %s" % site_domain)
            strong.add(THIRD_PARTY)
    for remote in remotes:
        if not _OCA_REPO.search(remote):
            signals.append("addons checkout's git remote: %s" % remote)
    if info.get("has_store_packaging"):
        signals.append("packaged for the App Store (static/description/index.html or manifest images)")
    if not author:
        signals.append("no author in the module list")
    if not info.get("on_disk", True):
        signals.append("no code found on the addons path")

    resolved = set(strong)
    if PAID_VENDOR in resolved:
        resolved.discard(THIRD_PARTY)
    if STUDIO_IMPORTED in resolved:
        resolved.discard(IN_HOUSE)
        resolved.discard(THIRD_PARTY)
    if len(resolved) == 1:
        bucket = resolved.pop()
        conflict = False
    else:
        bucket = UNCLEAR
        conflict = len(resolved) > 1
        if conflict:
            signals.append(
                "signals point to more than one origin (%s)"
                % ", ".join(BUCKET_LABELS[b] for b in sorted(resolved))
            )
        else:
            signals.append("no signal decides an origin")

    result = _result(bucket, signals, conflict)
    result["author"] = author or None
    result["oca_repo"] = repos[0] if repos else None
    result["vendor_site"] = vendor_site
    result["support"] = manifest.get("support") or None
    return result


def _result(bucket, signals, conflict):
    return {
        "bucket": bucket,
        "label": BUCKET_LABELS[bucket],
        "signals": signals,
        "conflict": conflict,
        "oca_repo": None,
        "vendor_site": None,
        "support": None,
    }


def target_action(result, name, target, footprint=None, removed_deps=()):
    """Plain-language note on what this module needs for the target series.

    No link is constructed: whether a module has a page or a branch for the
    target cannot be verified offline, so the note says where to look instead.
    `footprint` is a dict with bytes, files, views, menus, reports, dependents.
    `removed_deps` are Odoo modules it depends on that the target no longer has.
    """
    bucket = result["bucket"]
    fp = footprint or {}
    parts = []
    if bucket == OCA:
        repo = result.get("oca_repo")
        parts.append(
            "OCA module%s. Check whether the OCA has released it for Odoo %s."
            % (" (repository %s)" % repo if repo else "", target)
        )
    elif bucket in (PAID_VENDOR, THIRD_PARTY):
        who = result.get("author") or "the vendor"
        parts.append("Ask %s for a version for Odoo %s." % (who, target))
        if result.get("vendor_site"):
            parts.append("Website: %s." % result["vendor_site"])
        if result.get("support"):
            parts.append("Support: %s." % result["support"])
    elif bucket == STUDIO_IMPORTED:
        parts.append(
            "Studio or imported data, not code: it is re-applied on the target rather "
            "than ported. The individual customisations are in the Customisations tab."
        )
    elif bucket == IN_HOUSE:
        parts.append(
            "Written for this company: nobody else will port it, so your developers "
            "need to move it to Odoo %s. %s across %s files; %s screens, %s menus and "
            "%s reports; %s installed modules depend on it."
            % (
                target,
                human_size(fp.get("bytes")),
                _fmt(fp.get("files")),
                _fmt(fp.get("views")),
                _fmt(fp.get("menus")),
                _fmt(fp.get("reports")),
                _fmt(fp.get("dependents")),
            )
        )
    elif bucket in (ODOO_STANDARD, ODOO_ENTERPRISE):
        parts.append("Odoo's own module; it moves with the Odoo upgrade.")
    else:
        parts.append("Origin unclear: the signals that were found are listed. Check "
                     "with whoever installed it where it came from.")
    if removed_deps:
        parts.append(
            "It depends on %s, which %s not exist in Odoo %s (renamed, merged or removed)."
            % (", ".join(removed_deps), "does" if len(removed_deps) == 1 else "do", target)
        )
    return " ".join(parts)


def human_size(value):
    """Bytes as kB / MB / GB text."""
    if value is None:
        return "unknown size"
    value = float(value) / 1024.0
    for unit in ("kB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024.0


def _fmt(value):
    if value is None:
        return "unknown"
    return "{:,}".format(value)
