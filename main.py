
import ipaddress
import json
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

app = FastAPI(
    title="Wishlist API",
    description="API para extraer información de productos",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://127.0.0.1:8081",
        "http://localhost:19006",
        "http://127.0.0.1:19006",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

class ProductRequest(BaseModel):
    url: HttpUrl


def validate_public_url(url: str) -> None:
    """Evita acceder a localhost y direcciones IP privadas."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Solo se permiten URLs HTTP o HTTPS")

    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "URL no válida")

    host = parsed.hostname.lower()

    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise HTTPException(400, "Dominio no permitido")

    try:
        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        if not addresses:
            raise HTTPException(400, "No se pudo resolver el dominio")

        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise HTTPException(400, "Dirección de red no permitida")
    except HTTPException:
        raise
    except (OSError, ValueError):
        raise HTTPException(400, "No se pudo validar el dominio")


def get_meta(soup, *names):
    for name in names:
        tag = (
            soup.find("meta", attrs={"property": name})
            or soup.find("meta", attrs={"name": name})
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def find_product_jsonld(soup):
    for script in soup.find_all(
        "script", attrs={"type": "application/ld+json"}
    ):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        pending = data if isinstance(data, list) else [data]

        while pending:
            obj = pending.pop(0)

            if not isinstance(obj, dict):
                continue

            graph = obj.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)

            types = obj.get("@type", [])
            if isinstance(types, str):
                types = [types]

            if "Product" not in types:
                continue

            offers = obj.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}

            image = obj.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")

            return {
                "name": obj.get("name"),
                "imageUrl": image,
                "price": offers.get("price"),
                "currency": offers.get("priceCurrency"),
            }

    return {}


@app.get("/")
def home():
    return {"message": "Wishlist API funcionando"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/products/extract")
async def extract_product(body: ProductRequest):
    url = str(body.url)
    validate_public_url(url)

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WishlistApp/1.0)"
            },
        ) as client:
            response = await client.get(url)

        if response.status_code in (301, 302, 303, 307, 308):
            raise HTTPException(
                422,
                "La tienda redirige el enlace. Usa la URL final del producto.",
            )

        response.raise_for_status()

        if "text/html" not in response.headers.get("content-type", "").lower():
            raise HTTPException(422, "La URL no contiene una página HTML")

        if len(response.content) > 3_000_000:
            raise HTTPException(413, "La página supera el tamaño permitido")

    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(422, "No se pudo consultar la tienda")

    soup = BeautifulSoup(response.content, "html.parser")
    product = find_product_jsonld(soup)

    name = (
        product.get("name")
        or get_meta(soup, "og:title", "twitter:title")
    )

    image = (
        product.get("imageUrl")
        or get_meta(soup, "og:image", "twitter:image")
    )

    price = product.get("price")
    currency = product.get("currency")

    if price is None:
        price = get_meta(
            soup,
            "product:price:amount",
            "og:price:amount",
        )

    if currency is None:
        currency = get_meta(
            soup,
            "product:price:currency",
            "og:price:currency",
        )

    try:
        price = float(price) if price is not None else None
        if price is not None and price < 0:
            price = None
    except (ValueError, TypeError):
        price = None

    if not name:
        raise HTTPException(
            422,
            "No se encontró el nombre del producto. "
            "Puedes introducirlo manualmente.",
        )

    return {
        "name": name,
        "imageUrl": image,
        "price": price,
        "currency": currency,
        "productUrl": url,
    }
