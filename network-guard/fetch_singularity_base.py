"""Fetch a digest-verified public OCI image as a legacy Docker archive.

No Docker daemon, root privilege or archive extraction is used here.
Singularity performs the subsequent unpacking.
"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path('/mnt/example/runtime-data')
REPOSITORY = 'library/python'
TAG = '3.11-slim-bookworm'
REGISTRY = 'https://registry-1.docker.io/v2/' + REPOSITORY
ACCEPT = ', '.join(('application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json'))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return 'sha256:' + h.hexdigest()


def main():
    cache = ROOT / 'cache' / 'verified-python'
    cache.mkdir(parents=True, exist_ok=True)
    auth = 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull'
    with urllib.request.urlopen(auth, timeout=30) as response:
        token = json.load(response)['token']

    def request(url):
        return urllib.request.urlopen(urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + token, 'Accept': ACCEPT}), timeout=90)

    with request(REGISTRY + '/manifests/' + TAG) as response:
        index_bytes = response.read()
    index = json.loads(index_bytes)
    if 'manifests' in index:
        match = [m for m in index['manifests'] if m.get('platform', {}).get('os') == 'linux'
                 and m.get('platform', {}).get('architecture') == 'amd64']
        if len(match) != 1:
            raise RuntimeError('Expected one linux/amd64 manifest')
        wanted = match[0]['digest']
        with request(REGISTRY + '/manifests/' + wanted) as response:
            manifest_bytes = response.read()
        if 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest() != wanted:
            raise RuntimeError('Manifest digest mismatch')
    else:
        manifest_bytes = index_bytes
        wanted = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)

    def blob(descriptor):
        name = descriptor['digest'].split(':', 1)[1]
        path = cache / name
        if path.exists() and digest(path) == descriptor['digest']:
            return path
        temporary = path.with_suffix('.part')
        with request(REGISTRY + '/blobs/' + descriptor['digest']) as response, temporary.open('wb') as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
        if temporary.stat().st_size != descriptor['size'] or digest(temporary) != descriptor['digest']:
            raise RuntimeError('Blob verification failed')
        temporary.replace(path)
        return path

    config_path = blob(manifest['config'])
    config = json.loads(config_path.read_text())
    layers = []
    for number, descriptor in enumerate(manifest['layers']):
        packed = blob(descriptor)
        layer = cache / (packed.name + '.tar')
        expected = config['rootfs']['diff_ids'][number]
        if not layer.exists() or digest(layer) != expected:
            temporary = layer.with_suffix('.tar.part')
            with gzip.open(packed, 'rb') as source, temporary.open('wb') as output:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
            if digest(temporary) != expected:
                raise RuntimeError('Uncompressed layer verification failed')
            temporary.replace(layer)
        layers.append(layer)
        print(json.dumps({'verified_layer': number + 1, 'layers': len(manifest['layers'])}), flush=True)
    archive = ROOT / 'build' / 'python-base.tar'
    with tarfile.open(archive, 'w') as tar:
        tar.add(config_path, arcname='config.json', recursive=False)
        for layer in layers:
            tar.add(layer, arcname=layer.name, recursive=False)
        metadata = json.dumps([{'Config': 'config.json', 'RepoTags': ['python:' + TAG],
                                'Layers': [layer.name for layer in layers]}]).encode()
        item = tarfile.TarInfo('manifest.json')
        item.size = len(metadata)
        tar.addfile(item, io.BytesIO(metadata))
    metadata = {'source': 'docker.io/library/python:' + TAG, 'platform': 'linux/amd64',
                'manifest_digest': wanted, 'archive_sha256': digest(archive), 'archive': str(archive)}
    (ROOT / 'build' / 'base-image.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps(metadata), flush=True)


if __name__ == '__main__':
    main()
