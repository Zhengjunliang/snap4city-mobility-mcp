@mcp.tool(name="tpl_agencies", tags={"tpl"}, meta={"tags": ["tpl"]})
async def tpl_agencies(
    authentication: Annotated[Optional[str], Field(default=None, description="Bearer token for authorization if required.")] = None
) -> Dict[str, Any]:
    """
    Retrieves all public transport agencies available on Snap4City.
    Returns an array of agency objects with names and URIs.
    """
    try:
        base_url = "https://www.snap4city.org/superservicemap/api/v1/tpl/agencies/"
        request_url = base_url + (f"?accessToken={quote(authentication)}" if authentication else "")
        payload, err, http_meta = await _safe_get(request_url, {"Connection": "close"}, timeout=60.0)
        if err:
            return create_error(err, meta=http_meta)
        agencies = payload.get("Agencies", [])
        desc = _describe_payload(payload, hint="tpl")
        return create_success(agencies, total=len(agencies), meta=desc)
    except Exception as e:
        return create_error(f"Internal Tool Error: {type(e).__name__}: {e}")
 
 
@mcp.tool(name="tpl_lines", tags={"tpl"}, meta={"tags": ["tpl"]})
async def tpl_lines(
    agency: Annotated[str, Field(description="URI of the transport agency. Mandatory.")],
    authentication: Annotated[Optional[str], Field(default=None, description="Bearer token for authorization if required.")] = None
) -> Dict[str, Any]:
    """
    Lists all public transport lines for a given agency.
    Returns an array of line objects with long name, short name, and URI.
    """
    try:
        def q(val): return quote(str(val), safe=';,/?:@&=+$-_.!~*\'()#')
        request_url = f"https://www.snap4city.org/superservicemap/api/v1/tpl/bus-lines/?agency={q(agency)}&appID=iotapp"
        headers = {"Connection": "close"}
        if authentication: headers["Authorization"] = f"Bearer {authentication}"
        payload, err, http_meta = await _safe_get(request_url, headers, timeout=60.0)
        if err:
            return create_error(err, meta=http_meta)
        lines = payload.get("BusLines", [])
 
        groups: Dict[str, list] = {}
        for line in lines:
            groups.setdefault(line.get("shortName", ""), []).append(line)
 
        async def _is_live_uri(uri: str) -> bool:
            h = {"Connection": "close", "Accept": "text/html"}
            if authentication: h["Authorization"] = f"Bearer {authentication}"
            try:
                async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                    resp = await client.get(uri, headers=h)
                return "<td>" in resp.text
            except Exception:
                return False
 
        probe_pairs = [(sn, c) for sn, cands in groups.items() for c in cands if len(cands) > 1]
        if probe_pairs:
            probe_results = await asyncio.gather(*[_is_live_uri(c["uri"]) for _, c in probe_pairs])
            live_uris = {c["uri"] for (_, c), ok in zip(probe_pairs, probe_results) if ok}
        else:
            live_uris = set()
 
        deduped = []
        for sn, candidates in groups.items():
            if len(candidates) == 1:
                deduped.append(candidates[0])
            else:
                winner = next((c for c in candidates if c["uri"] in live_uris), candidates[-1])
                deduped.append(winner)
 
        desc = _describe_payload(payload, hint="tpl")
        return create_success(deduped, total=len(deduped), meta=desc)
    except Exception as e:
        return create_error(f"Internal Tool Error: {type(e).__name__}: {e}")
 
@mcp.tool(name="tpl_routes_by_line", tags={"tpl"}, meta={"tags": ["tpl"]})
async def tpl_routes_by_line(
    line: Annotated[str, Field(description="URI a line (if URI is provided the agency is not needed).")],
    agency: Annotated[Optional[str], Field(default=None, description="URI of the agency.")] = None,
    authentication: Annotated[Optional[str], Field(default=None, description="Bearer token for authorization if required.")] = None
) -> Dict[str, Any]:
    """
    Lists all routes (directions) for a transport line, including WKT polyline geometry.
    Returns an array of route objects.
    """
    try:
        def q(val): return quote(str(val), safe=';,/?:@&=+$-_.!~*\'()#')
        qs = f"?line={q(line)}&geometry=true&appID=iotapp"
        if agency: qs += f"&agency={q(agency)}"
        request_url = "https://www.snap4city.org/superservicemap/api/v1/tpl/bus-routes/" + qs
        headers = {"Connection": "close"}
        if authentication: headers["Authorization"] = f"Bearer {authentication}"
        payload, err, http_meta = await _safe_get(request_url, headers, timeout=60.0)
        if err:
            return create_error(err, meta=http_meta)
        routes = payload.get("BusRoutes", [])
        desc = _describe_payload(payload, hint="tpl")
        return create_success(routes, total=len(routes), meta=desc)
    except Exception as e:
        return create_error(f"Internal Tool Error: {type(e).__name__}: {e}")
 
@mcp.tool(name="tpl_stops_by_route", tags={"tpl"}, meta={"tags": ["tpl"]})
async def tpl_stops_by_route(
    route: Annotated[str, Field(description="URI of the route whose stops are to be retrieved.")],
    authentication: Annotated[Optional[str], Field(default=None, description="Bearer token for authorization if required.")] = None
) -> Dict[str, Any]:
    """
    Lists all stops along a route in order.
    Returns a dict with 'service_uris' (flat URI list) and 'features' (full GeoJSON features).
    """
    try:
        def q(val): return quote(str(val), safe=';,/?:@&=+$-_.!~*\'()#')
        request_url = f"https://www.snap4city.org/superservicemap/api/v1/tpl/bus-stops/?route={q(route)}&geometry=true&appID=iotapp"
        headers = {"Connection": "close"}
        if authentication: headers["Authorization"] = f"Bearer {authentication}"
        payload, err, http_meta = await _safe_get(request_url, headers, timeout=60.0)
        if err:
            return create_error(err, meta=http_meta)
        bus_stops = payload.get("BusStops", {})
        features = bus_stops.get("features", []) if isinstance(bus_stops, dict) else []
        service_uris = [f.get("properties", {}).get("serviceUri") for f in features if f.get("properties", {}).get("serviceUri")]
        desc = _describe_payload({"type": "FeatureCollection", "features": features})
        return create_success({"service_uris": service_uris, "features": features}, total=len(features), meta=desc)
    except Exception as e:
        return create_error(f"Internal Tool Error: {type(e).__name__}: {e}")