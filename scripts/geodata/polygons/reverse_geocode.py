# -*- coding: utf-8 -*-
'''
reverse_geocoder.py
-------------------

In-memory reverse geocoder using polygons from Quattroshapes or OSM.
This should be useful for filling in the blanks both in constructing
training data from OSM addresses and for OpenVenues.

Usage:
    python reverse_geocode.py -o /data/quattroshapes/rtree/reverse -q /data/quattroshapes/
    python reverse_geocode.py -o /data/quattroshapes/rtree/reverse -a /data/osm/planet-admin-borders.osm
'''
import argparse
import logging
import operator
import os
import re
import requests
import shutil
import six
import subprocess
import sys
import tempfile

from functools import partial

this_dir = os.path.realpath(os.path.dirname(__file__))
sys.path.append(os.path.realpath(os.path.join(os.pardir, os.pardir)))

from geodata.coordinates.conversion import latlon_to_decimal
from geodata.encoding import safe_decode
from geodata.file_utils import ensure_dir, download_file
from geodata.i18n.unicode_properties import get_chars_by_script
from geodata.i18n.word_breaks import ideographic_scripts
from geodata.names.deduping import NameDeduper
from geodata.osm.extract import parse_osm, OSM_NAME_TAGS, WAY_OFFSET, RELATION_OFFSET
from geodata.osm.osm_admin_boundaries import OSMAdminPolygonReader, OSMSubdivisionPolygonReader, OSMBuildingPolygonReader
from geodata.polygons.index import *
from geodata.statistics.tf_idf import IDFIndex

from geodata.text.tokenize import tokenize, token_types
from geodata.text.normalize import *

from shapely.topology import TopologicalError

decode_latin1 = partial(safe_decode, encoding='latin1')


def str_id(v):
    v = int(v)
    if v <= 0:
        return None
    return str(v)


class NeighborhoodDeduper(NameDeduper):
    # Lossless conversions only
    replacements = {
        u'saint': u'st',
        u'and': u'&',
    }

    discriminative_words = set([
        # Han numbers
        u'〇', u'一',
        u'二', u'三',
        u'四', u'五',
        u'六', u'七',
        u'八', u'九',
        u'十', u'百',
        u'千', u'万',
        u'億', u'兆',
        u'京', u'第',

        # Roman numerals
        u'i', u'ii',
        u'iii', u'iv',
        u'v', u'vi',
        u'vii', u'viii',
        u'ix', u'x',
        u'xi', u'xii',
        u'xiii', u'xiv',
        u'xv', u'xvi',
        u'xvii', u'xviii',
        u'xix', u'xx',

        # English directionals
        u'north', u'south',
        u'east', u'west',
        u'northeast', u'northwest',
        u'southeast', u'southwest',

        # Spanish, Portguese and Italian directionals
        u'norte', u'nord', u'sur', u'sul', u'sud',
        u'est', u'este', u'leste', u'oeste', u'ovest',

        # New in various languages
        u'new',
        u'nova',
        u'novo',
        u'nuevo',
        u'nueva',
        u'nuovo',
        u'nuova',

        # Qualifiers
        u'heights',
        u'hills',

        u'upper', u'lower',
        u'little', u'great',

        u'park',
        u'parque',

        u'village',

    ])

    stopwords = set([
        u'cp',
        u'de',
        u'la',
        u'urbanizacion',
        u'do',
        u'da',
        u'dos',
        u'del',
        u'community',
        u'bairro',
        u'barrio',
        u'le',
        u'el',
        u'mah',
        u'раион',
        u'vila',
        u'villa',
        u'kampung',
        u'ahupua`a',

    ])


class ZetashapesReverseGeocoder(GeohashPolygonIndex):
    simplify_tolerance = 0.00001
    preserve_topology = True
    persistent_polygons = False
    cache_size = 0

    SCRATCH_DIR = '/tmp'

    supplemental_geojson_urls = [
        ('http://catalog.civicdashboards.com/dataset/eea7c03e-9917-40b0-bba5-82e8e37d6739/resource/91778048-3c58-449c-a3f9-365ed203e914/download/06463a12c2104adf86335df0170c25e3pediacitiesnycneighborhoods.geojson', 'pediacities_nyc.geojson'),
    ]

    NEIGHBORHOODS_REPO = 'https://github.com/blackmad/neighborhoods'

    @classmethod
    def clone_repo(cls, path):
        subprocess.check_call(['rm', '-rf', path])
        subprocess.check_call(['git', 'clone', cls.NEIGHBORHOODS_REPO, path])

    @classmethod
    def create_neighborhoods_index(cls):
        scratch_dir = cls.SCRATCH_DIR
        repo_path = os.path.join(scratch_dir, 'neighborhoods')
        cls.clone_repo(repo_path)

        neighborhoods_dir = os.path.join(scratch_dir, 'neighborhoods', 'index')
        ensure_dir(neighborhoods_dir)

        index = cls(save_dir=neighborhoods_dir)

        have_geonames = set()
        is_neighborhood = set()

        for filename in os.listdir(repo_path):
            path = os.path.join(repo_path, filename)
            base_name = filename.split('.')[0].split('gn-')[-1]
            if filename.endswith('.geojson') and filename.startswith('gn-'):
                have_geonames.add(base_name)
            elif filename.endswith('metadata.json'):
                data = json.load(open(os.path.join(repo_path, filename)))
                if data.get('neighborhoodNoun', [None])[0] in (None, 'rione'):
                    is_neighborhood.add(base_name)

        for filename in os.listdir(repo_path):
            if not filename.endswith('.geojson'):
                continue
            base_name = filename.rsplit('.geojson')[0]
            if base_name in have_geonames:
                f = open(os.path.join(repo_path, 'gn-{}'.format(filename)))
            elif base_name in is_neighborhood:
                f = open(os.path.join(repo_path, filename))
            else:
                continue
            index.add_geojson_like_file(json.load(f)['features'])

        for url, filename in cls.supplemental_geojson_urls:
            local_path = os.path.join(scratch_dir, filename)
            download_file(url, local_path)
            index.add_geojson_like_file(json.load(open(local_path))['features'])

        return index


class NeighborhoodReverseGeocoder(RTreePolygonIndex):
    '''
    Neighborhoods are very important in cities like NYC, SF, Chicago, London
    and many others. We want the address parser to be trained with addresses
    that sufficiently capture variations in address patterns, including
    neighborhoods. Quattroshapes neighborhood data (in the US at least)
    is not great in terms of names, mostly becasue GeoPlanet has so many
    incorrect names. The neighborhoods project, also known as Zetashapes
    has very accurate polygons with correct names, but only for a handful
    of cities. OSM usually lists neighborhoods and some other local admin
    areas like boroughs as points rather than polygons.

    This index merges all of the above data sets in prioritized order
    (Zetashapes > OSM > Quattroshapes) to provide unified point-in-polygon
    tests for neighborhoods. The properties vary by source but each has
    source has least a "name" key which in practice is what we care about.
    '''

    SCRATCH_DIR = '/tmp'

    PRIORITIES_FILENAME = 'priorities.json'

    DUPE_THRESHOLD = 0.9

    source_priorities = {
        'zetashapes': 0,     # Best names/polygons
        'osm_zeta': 1,       # OSM names matched with Zetashapes polygon
        'osm_quattro': 2,    # OSM names matched with Quattroshapes polygon
        'quattroshapes': 3,  # Good results in some countries/areas
    }

    level_priorities = {
        'neighborhood': 0,
        'local_admin': 1,
    }

    regex_replacements = [
        # Paris arrondissements, listed like "PARIS-1ER-ARRONDISSEMENT" in Quqttroshapes
        (re.compile('^paris-(?=[\d])', re.I), ''),
    ]

    @classmethod
    def count_words(cls, s):
        doc = defaultdict(int)
        for t, c in NeighborhoodDeduper.content_tokens(s):
            doc[t] += 1
        return doc

    @classmethod
    def create_from_osm_and_quattroshapes(cls, filename, quattroshapes_dir, output_dir, scratch_dir=SCRATCH_DIR):
        '''
        Given an OSM file (planet or some other bounds) containing neighborhoods
        as points (some suburbs have boundaries)

        and their dependencies, create an R-tree index for coarse-grained
        reverse geocoding.

        Note: the input file is expected to have been created using
        osmfilter. Use fetch_osm_address_data.sh for planet or copy the
        admin borders commands if using other geometries.
        '''
        index = cls(save_dir=output_dir)

        ensure_dir(scratch_dir)

        logger = logging.getLogger('neighborhoods')

        qs_scratch_dir = os.path.join(scratch_dir, 'qs_neighborhoods')
        ensure_dir(qs_scratch_dir)
        logger.info('Creating Quattroshapes neighborhoods')

        qs = QuattroshapesNeighborhoodsReverseGeocoder.create_neighborhoods_index(quattroshapes_dir, qs_scratch_dir)
        logger.info('Creating Zetashapes neighborhoods')
        zs = ZetashapesReverseGeocoder.create_neighborhoods_index()

        logger.info('Creating IDF index')
        idf = IDFIndex()

        char_scripts = get_chars_by_script()

        for idx in (zs, qs):
            for i in xrange(idx.i):
                props = idx.get_properties(i)
                name = props.get('name')
                if name is not None:
                    doc = cls.count_words(name)
                    idf.update(doc)

        for key, attrs, deps in parse_osm(filename):
            for k, v in six.iteritems(attrs):
                if any((k.startswith(name_key) for name_key in OSM_NAME_TAGS)):
                    doc = cls.count_words(v)
                    idf.update(doc)

        qs.matched = [False] * qs.i
        zs.matched = [False] * zs.i

        logger.info('Matching OSM points to neighborhood polygons')
        # Parse OSM and match neighborhood/suburb points to Quattroshapes/Zetashapes polygons
        num_polys = 0
        for node_id, attrs, deps in parse_osm(filename):
            try:
                lat, lon = latlon_to_decimal(attrs['lat'], attrs['lon'])
            except ValueError:
                continue

            osm_name = attrs.get('name')
            if not osm_name:
                continue

            is_neighborhood = attrs.get('place') == 'neighbourhood'

            ranks = []
            osm_names = []

            for key in OSM_NAME_TAGS:
                name = attrs.get(key)
                if name:
                    osm_names.append(name)

            for name_key in OSM_NAME_TAGS:
                osm_names.extend([v for k, v in six.iteritems(attrs) if k.startswith('{}:'.format(name_key))])

            for idx in (zs, qs):
                candidates = idx.get_candidate_polygons(lat, lon, return_all=True)

                if candidates:
                    max_sim = 0.0
                    arg_max = None

                    normalized_qs_names = {}

                    for osm_name in osm_names:

                        contains_ideographs = any(((char_scripts[ord(c)] or '').lower() in ideographic_scripts
                                                   for c in safe_decode(osm_name)))

                        for i in candidates:
                            props = idx.get_properties(i)
                            name = normalized_qs_names.get(i)
                            if not name:
                                name = props.get('name')
                                if not name:
                                    continue
                                for pattern, repl in cls.regex_replacements:
                                    name = pattern.sub(repl, name)
                                normalized_qs_names[i] = name

                            if is_neighborhood and idx is qs and props.get(QuattroshapesReverseGeocoder.LEVEL) != 'neighborhood':
                                continue

                            if not contains_ideographs:
                                sim = NeighborhoodDeduper.compare(osm_name, name, idf)
                            else:
                                # Many Han/Hangul characters are common, shouldn't use IDF
                                sim = NeighborhoodDeduper.compare_ideographs(osm_name, name)

                            if sim > max_sim:
                                max_sim = sim
                                poly = idx.get_polygon(i)
                                arg_max = (max_sim, props, poly.context, idx, i)

                    if arg_max:
                        ranks.append(arg_max)

            ranks.sort(key=operator.itemgetter(0), reverse=True)
            if ranks and ranks[0][0] >= cls.DUPE_THRESHOLD:
                score, props, poly, idx, i = ranks[0]

                if idx is zs:
                    attrs['polygon_type'] = 'neighborhood'
                    source = 'osm_zeta'
                else:
                    level = props.get(QuattroshapesReverseGeocoder.LEVEL, None)
                    source = 'osm_quattro'
                    if level == 'neighborhood':
                        attrs['polygon_type'] = 'neighborhood'
                    else:
                        attrs['polygon_type'] = 'local_admin'

                attrs['source'] = source
                index.index_polygon(poly)
                index.add_polygon(poly, attrs)
                idx.matched[i] = True

            num_polys += 1
            if num_polys % 1000 == 0 and num_polys > 0:
                logger.info('did {} neighborhoods'.format(num_polys))

        for idx, source in ((zs, 'zetashapes'), (qs, 'quattroshapes')):
            for i in xrange(idx.i):
                props = idx.get_properties(i)
                poly = idx.get_polygon(i)
                if idx.matched[i]:
                    continue
                props['source'] = source
                if idx is zs or props.get(QuattroshapesReverseGeocoder.LEVEL, None) == 'neighborhood':
                    props['polygon_type'] = 'neighborhood'
                else:
                    # We don't actually care about local admin polygons unless they match OSM
                    continue
                index.index_polygon(poly.context)
                index.add_polygon(poly.context, props)

        return index

    def setup(self):
        self.priorities = []

    def index_polygon_properties(self, properties):
        self.priorities.append((properties['polygon_type'], self.source_priorities[properties['source']]))

    def load_polygon_properties(self, d):
        self.priorities = json.load(open(os.path.join(d, self.PRIORITIES_FILENAME)))

    def save_polygon_properties(self, d):
        json.dump(self.priorities, open(os.path.join(d, self.PRIORITIES_FILENAME), 'w'))

    def priority(self, i):
        return self.priorities[i]

    def get_candidate_polygons(self, lat, lon):
        candidates = super(NeighborhoodReverseGeocoder, self).get_candidate_polygons(lat, lon)
        return sorted(candidates, key=self.priority)


class QuattroshapesReverseGeocoder(RTreePolygonIndex):
    '''
    Quattroshapes polygons, for levels up to localities, are relatively
    accurate and provide concordance with GeoPlanet and in some cases
    GeoNames (which is used in other parts of this project).
    '''
    COUNTRIES_FILENAME = 'qs_adm0.shp'
    ADMIN1_FILENAME = 'qs_adm1.shp'
    ADMIN1_REGION_FILENAME = 'qs_adm1_region.shp'
    ADMIN2_FILENAME = 'qs_adm2.shp'
    ADMIN2_REGION_FILENAME = 'qs_adm2_region.shp'
    LOCAL_ADMIN_FILENAME = 'qs_localadmin.shp'
    LOCALITIES_FILENAME = 'qs_localities.shp'
    NEIGHBORHOODS_FILENAME = 'qs_neighborhoods.shp'

    COUNTRY = 'adm0'
    ADMIN1 = 'adm1'
    ADMIN1_REGION = 'adm1_region'
    ADMIN2 = 'adm2'
    ADMIN2_REGION = 'adm2_region'
    LOCAL_ADMIN = 'localadmin'
    LOCALITY = 'locality'
    NEIGHBORHOOD = 'neighborhood'

    PRIORITIES_FILENAME = 'priorities.json'

    persistent_polygons = True
    cache_size = 100000

    sorted_levels = (COUNTRY,
                     ADMIN1_REGION,
                     ADMIN1,
                     ADMIN2_REGION,
                     ADMIN2,
                     LOCAL_ADMIN,
                     LOCALITY,
                     NEIGHBORHOOD,
                     )

    sort_levels = {k: i for i, k in enumerate(sorted_levels)}

    NAME = 'name'
    CODE = 'code'
    LEVEL = 'level'
    GEONAMES_ID = 'geonames_id'
    WOE_ID = 'woe_id'

    polygon_properties = {
        COUNTRIES_FILENAME: {
            NAME: ('qs_a0', safe_decode),
            CODE: ('qs_iso_cc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        ADMIN1_FILENAME: {
            NAME: ('qs_a1', safe_decode),
            CODE: ('qs_a1_lc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        ADMIN1_REGION_FILENAME: {
            NAME: ('qs_a1r', safe_decode),
            CODE: ('qs_a1r_lc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        ADMIN2_FILENAME: {
            NAME: ('qs_a2', decode_latin1),
            CODE: ('qs_a2_lc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        ADMIN2_REGION_FILENAME: {
            NAME: ('qs_a2r', safe_decode),
            CODE: ('qs_a2r_lc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        LOCAL_ADMIN_FILENAME: {
            NAME: ('qs_la', safe_decode),
            CODE: ('qs_la_lc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str_id),
            WOE_ID: ('qs_woe_id', str_id),
        },
        LOCALITIES_FILENAME: {
            NAME: ('qs_loc', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('qs_gn_id', str),
            WOE_ID: ('qs_woe_id', str),
        },
        NEIGHBORHOODS_FILENAME: {
            NAME: ('name', safe_decode),
            CODE: ('name_en', safe_decode),
            LEVEL: ('qs_level', safe_decode),
            GEONAMES_ID: ('gn_id', str_id),
            WOE_ID: ('woe_id', str_id),
        }
    }

    @classmethod
    def create_from_shapefiles(cls,
                               input_files,
                               output_dir,
                               index_filename=None,
                               polys_filename=DEFAULT_POLYS_FILENAME,
                               use_all_props=False):

        index = cls(save_dir=output_dir, index_filename=index_filename)

        for input_file in input_files:
            f = fiona.open(input_file)

            filename = os.path.split(input_file)[-1]

            aliases = cls.polygon_properties.get(filename)

            if not use_all_props:
                include_props = aliases
            else:
                include_props = None

            for rec in f:
                if not rec or not rec.get('geometry') or 'type' not in rec['geometry']:
                    continue

                properties = rec['properties']

                if filename == cls.NEIGHBORHOODS_FILENAME:
                    properties['qs_level'] = 'neighborhood'

                have_all_props = False
                for k, (prop, func) in aliases.iteritems():
                    v = properties.get(prop, None)
                    if v is not None:
                        try:
                            properties[k] = func(v)
                        except Exception:
                            break
                else:
                    have_all_props = True
                if not have_all_props or not properties.get(cls.NAME):
                    continue

                poly_type = rec['geometry']['type']
                if poly_type == 'Polygon':
                    poly = Polygon(rec['geometry']['coordinates'][0])
                    index.index_polygon(poly)
                    poly = index.simplify_polygon(poly)
                    index.add_polygon(poly, dict(rec['properties']), include_only_properties=include_props)
                elif poly_type == 'MultiPolygon':
                    polys = []
                    for coords in rec['geometry']['coordinates']:
                        poly = Polygon(coords[0])
                        polys.append(poly)
                        index.index_polygon(poly)

                    multi_poly = index.simplify_polygon(MultiPolygon(polys))
                    index.add_polygon(multi_poly, dict(rec['properties']), include_only_properties=include_props)
                else:
                    continue

        return index

    @classmethod
    def create_with_quattroshapes(cls, quattroshapes_dir,
                                  output_dir,
                                  index_filename=None,
                                  polys_filename=DEFAULT_POLYS_FILENAME):

        admin0_filename = os.path.join(quattroshapes_dir, cls.COUNTRIES_FILENAME)
        admin1_filename = os.path.join(quattroshapes_dir, cls.ADMIN1_FILENAME)
        admin1r_filename = os.path.join(quattroshapes_dir, cls.ADMIN1_REGION_FILENAME)
        admin2_filename = os.path.join(quattroshapes_dir, cls.ADMIN2_FILENAME)
        admin2r_filename = os.path.join(quattroshapes_dir, cls.ADMIN2_REGION_FILENAME)
        localities_filename = os.path.join(quattroshapes_dir, cls.LOCALITIES_FILENAME)

        return cls.create_from_shapefiles([admin0_filename, admin1_filename, admin1r_filename,
                                          admin2_filename, admin2r_filename,
                                          localities_filename],
                                          output_dir, index_filename=index_filename,
                                          polys_filename=polys_filename)

    def setup(self):
        self.priorities = []

    def index_polygon_properties(self, properties):
        self.priorities.append(self.sort_levels.get(properties[self.LEVEL], 0))

    def load_polygon_properties(self, d):
        self.priorities = json.load(open(os.path.join(d, self.PRIORITIES_FILENAME)))

    def save_polygon_properties(self, d):
        json.dump(self.priorities, open(os.path.join(d, self.PRIORITIES_FILENAME), 'w'))

    def sort_level(self, i):
        return self.priorities[i]

    def get_candidate_polygons(self, lat, lon):
        candidates = super(QuattroshapesReverseGeocoder, self).get_candidate_polygons(lat, lon)
        return sorted(candidates, key=self.sort_level, reverse=True)


class QuattroshapesNeighborhoodsReverseGeocoder(GeohashPolygonIndex, QuattroshapesReverseGeocoder):
    persistent_polygons = False
    cache_size = None

    @classmethod
    def create_neighborhoods_index(cls, quattroshapes_dir,
                                   output_dir,
                                   index_filename=None,
                                   polys_filename=DEFAULT_POLYS_FILENAME):
        local_admin_filename = os.path.join(quattroshapes_dir, cls.LOCAL_ADMIN_FILENAME)
        neighborhoods_filename = os.path.join(quattroshapes_dir, cls.NEIGHBORHOODS_FILENAME)
        return cls.create_from_shapefiles([local_admin_filename, neighborhoods_filename],
                                          output_dir, index_filename=index_filename,
                                          polys_filename=polys_filename)


class OSMReverseGeocoder(RTreePolygonIndex):
    '''
    OSM has among the best, most carefully-crafted, accurate administrative
    polygons in the business in addition to using local language naming
    conventions which is desirable for creating a truly multilingual address
    parser.

    The storage of these polygons is byzantine. See geodata.osm.osm_admin_boundaries
    for more details.

    Suffice to say, this reverse geocoder builds an R-tree index on OSM planet
    in a reasonable amount of memory using arrays of C integers and binary search
    for the dependency lookups and Tarjan's algorithm for finding strongly connected
    components to stitch together the polygons.
    '''

    ADMIN_LEVEL = 'admin_level'

    ADMIN_LEVELS_FILENAME = 'admin_levels.json'

    polygon_reader = OSMAdminPolygonReader

    persistent_polygons = True
    # Cache everything
    cache_size = 1000000
    simplify_polygons = True

    include_property_patterns = set([
        'name',
        'name:*',
        'int_name',
        'official_name',
        'official_name:*',
        'alt_name',
        'alt_name:*',
        'short_name',
        'short_name:*',
        'admin_level',
        'place',
        'wikipedia',
        'wikipedia:*',
    ])

    @classmethod
    def create_from_osm_file(cls, filename, output_dir,
                             index_filename=None,
                             polys_filename=DEFAULT_POLYS_FILENAME):
        '''
        Given an OSM file (planet or some other bounds) containing relations
        and their dependencies, create an R-tree index for coarse-grained
        reverse geocoding.

        Note: the input file is expected to have been created using
        osmfilter. Use fetch_osm_address_data.sh for planet or copy the
        admin borders commands if using other bounds.
        '''
        index = cls(save_dir=output_dir, index_filename=index_filename)

        reader = cls.polygon_reader(filename)
        polygons = reader.polygons()

        logger = logging.getLogger('osm.reverse_geocode')

        for element_id, props, outer_polys, inner_polys in polygons:
            props = {k: v for k, v in six.iteritems(props)
                     if k in cls.include_property_patterns or (six.u(':') in k and
                     six.u('{}:*').format(k.split(six.u(':'), 1)[0]) in cls.include_property_patterns)}

            if element_id >= RELATION_OFFSET:
                props['type'] = 'relation'
                element_id -= RELATION_OFFSET
            elif element_id >= WAY_OFFSET:
                props['type'] = 'way'
                element_id -= WAY_OFFSET
            else:
                props['type'] = 'node'

            props['id'] = element_id

            if inner_polys and not outer_polys:
                logger.warn('inner polygons with no outer')
                continue
            if len(outer_polys) == 1 and not inner_polys:
                poly = cls.to_polygon(outer_polys[0])
                if poly is None or not poly.bounds or len(poly.bounds) != 4:
                    continue
                if poly.type != 'MultiPolygon':
                    index.index_polygon(poly)
                else:
                    for p in poly:
                        index.index_polygon(p)
            else:
                multi = []
                inner = []
                # Validate inner polygons (holes)
                for p in inner_polys:
                    poly = cls.to_polygon(p)
                    if poly is None or not poly.bounds or len(poly.bounds) != 4:
                        continue
                    if not poly.is_valid:
                        poly = cls.fix_polygon(poly)
                        if poly is None or not poly.bounds or len(poly.bounds) != 4:
                            continue

                    if poly.type != 'MultiPolygon':
                        inner.append(poly)
                    else:
                        inner.extend(poly)

                # Validate outer polygons
                for p in outer_polys:
                    poly = cls.to_polygon(p)
                    if poly is None or not poly.bounds or len(poly.bounds) != 4:
                        continue

                    interior = []
                    try:
                        # Figure out which outer polygon contains each inner polygon
                        interior = [p2 for p2 in inner if poly.contains(p2)]
                    except TopologicalError:
                        continue

                    if interior:
                        # Polygon with holes constructor
                        poly = Polygon(p, [zip(*p2.exterior.coords.xy) for p2 in interior])
                        if poly is None or not poly.bounds or len(poly.bounds) != 4:
                            continue
                    # R-tree only stores the bounding box, so add the whole polygon
                    if poly.type != 'MultiPolygon':
                        index.index_polygon(poly)
                        multi.append(poly)
                    else:
                        for p in poly:
                            index.index_polygon(p)
                        multi.extend(poly)

                if len(multi) > 1:
                    poly = MultiPolygon(multi)
                elif multi:
                    poly = multi[0]
                else:
                    continue
            if index.simplify_polygons:
                poly = index.simplify_polygon(poly)
            index.add_polygon(poly, props)

        return index

    def setup(self):
        self.admin_levels = []

    def index_polygon_properties(self, properties):
        admin_level = properties.get(self.ADMIN_LEVEL, 0)
        try:
            admin_level = int(admin_level)
        except ValueError:
            admin_level = 0
        self.admin_levels.append(admin_level)

    def load_polygon_properties(self, d):
        self.admin_levels = json.load(open(os.path.join(d, self.ADMIN_LEVELS_FILENAME)))

    def save_polygon_properties(self, d):
        json.dump(self.admin_levels, open(os.path.join(d, self.ADMIN_LEVELS_FILENAME), 'w'))

    def sort_level(self, i):
        return self.admin_levels[i]

    def get_candidate_polygons(self, lat, lon):
        candidates = super(OSMReverseGeocoder, self).get_candidate_polygons(lat, lon)
        return sorted(candidates, key=self.sort_level, reverse=True)


class OSMSubdivisionReverseGeocoder(OSMReverseGeocoder):
    persistent_polygons = True
    cache_size = 10000
    simplify_polygons = False
    polygon_reader = OSMSubdivisionPolygonReader
    include_property_patterns = OSMReverseGeocoder.include_property_patterns | set(['landuse', 'place'])


class OSMBuildingReverseGeocoder(OSMReverseGeocoder):
    persistent_polygons = True
    cache_size = 10000
    simplify_polygons = False
    polygon_reader = OSMBuildingPolygonReader
    include_property_patterns = OSMReverseGeocoder.include_property_patterns | set(['building', 'building:levels', 'building:part', 'addr:*'])


if __name__ == '__main__':
    # Handle argument parsing here
    parser = argparse.ArgumentParser()

    parser.add_argument('-q', '--quattroshapes-dir',
                        help='Path to quattroshapes dir')

    parser.add_argument('-a', '--osm-admin-file',
                        help='Path to OSM borders file (with dependencies, .osm format)')

    parser.add_argument('-s', '--osm-subdivisions-file',
                        help='Path to OSM subdivisions file (with dependencies, .osm format)')

    parser.add_argument('-b', '--osm-building-polygons-file',
                        help='Path to OSM building polygons file (with dependencies, .osm format)')

    parser.add_argument('-n', '--osm-neighborhoods-file',
                        help='Path to OSM neighborhoods file (no dependencies, .osm format)')

    parser.add_argument('-o', '--out-dir',
                        default=os.getcwd(),
                        help='Output directory')

    logging.basicConfig(level=logging.INFO)

    args = parser.parse_args()
    if args.osm_admin_file:
        index = OSMReverseGeocoder.create_from_osm_file(args.osm_admin_file, args.out_dir)
    elif args.osm_subdivisions_file:
        index = OSMSubdivisionReverseGeocoder.create_from_osm_file(args.osm_subdivisions_file, args.out_dir)
    elif args.osm_building_polygons_file:
        index = OSMBuildingReverseGeocoder.create_from_osm_file(args.osm_building_polygons_file, args.out_dir)
    elif args.osm_neighborhoods_file and args.quattroshapes_dir:
        index = NeighborhoodReverseGeocoder.create_from_osm_and_quattroshapes(
            args.osm_neighborhoods_file,
            args.quattroshapes_dir,
            args.out_dir
        )
    elif args.quattroshapes_dir:
        index = QuattroshapesReverseGeocoder.create_with_quattroshapes(args.quattroshapes_dir, args.out_dir)
    else:
        parser.error('Must specify quattroshapes dir or osm admin borders file')

    index.save()
