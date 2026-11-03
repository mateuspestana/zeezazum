import unittest

from zeezazum.models import MediaKind
from zeezazum.platforms.instagram.parser import parse_posts_from_payload


def node(code, username, **extra):
    return {"code": code, "user": {"username": username}, **extra}


class PolarisShapeTests(unittest.TestCase):
    def test_display_uri_vira_midia_e_typename_define_tipo(self):
        payload = {
            "x": node("A1", "alice", display_uri="https://cdn/a.jpg", __typename="XIGPolarisPhotoMedia")
        }
        (post,) = parse_posts_from_payload(payload)
        self.assertEqual(post.media[0].kind, MediaKind.IMAGE)
        self.assertEqual(post.media[0].url, "https://cdn/a.jpg")
        self.assertEqual(post.metrics["media_type"], 1)
        self.assertEqual(post.metrics["media_type_label"], "photo")


class OwnerFilterTests(unittest.TestCase):
    def payload(self):
        return {
            "feed": [
                node("MINE", "Alice", display_uri="u1"),
                node("OTHER", "bob", display_uri="u2"),
                node("COLLAB", "bob", display_uri="u3", coauthor_producers=[{"username": "alice"}]),
            ]
        }

    def test_fallback_descarta_outros_perfis_mas_mantem_coautoria(self):
        codes = {p.shortcode for p in parse_posts_from_payload(self.payload(), "alice")}
        self.assertEqual(codes, {"MINE", "COLLAB"})

    def test_sem_handle_nao_filtra(self):
        self.assertEqual(len(parse_posts_from_payload(self.payload())), 3)

    def test_conexao_nomeada_nao_e_filtrada(self):
        payload = {
            "xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": node("T1", "alice", display_uri="u")}]
            }
        }
        self.assertEqual([p.shortcode for p in parse_posts_from_payload(payload, "alice")], ["T1"])


if __name__ == "__main__":
    unittest.main()
