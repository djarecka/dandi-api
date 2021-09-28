import os
from typing import Dict, List

from celery import shared_task
from celery.utils.log import get_task_logger
from django.conf import settings
from django.db.transaction import atomic
import jsonschema.exceptions

from dandiapi.api.checksum import calculate_sha256_checksum
from dandiapi.api.doi import delete_doi
from dandiapi.api.manifests import (
    write_assets_jsonld,
    write_assets_yaml,
    write_collection_jsonld,
    write_dandiset_jsonld,
    write_dandiset_yaml,
)
from dandiapi.api.models import Asset, AssetBlob, Version

if settings.DANDI_ALLOW_LOCALHOST_URLS:
    # If this environment variable is set, the pydantic model will allow URLs with localhost
    # in them. This is important for development and testing environments, where URLs will
    # frequently point to localhost.
    os.environ['DANDI_ALLOW_LOCALHOST_URLS'] = 'True'

import dandischema.exceptions
from dandischema.metadata import validate

logger = get_task_logger(__name__)


@shared_task
@atomic
def calculate_sha256(blob_id: int) -> None:
    logger.info('Starting sha256 calculation for blob %s', blob_id)
    asset_blob = AssetBlob.objects.get(blob_id=blob_id)

    sha256 = calculate_sha256_checksum(asset_blob.blob.storage, asset_blob.blob.name)
    logger.info('Calculated sha256 %s', sha256)

    # TODO: Run dandi-cli validation

    logger.info('Saving sha256 %s to blob %s', sha256, blob_id)

    asset_blob.sha256 = sha256
    asset_blob.save()

    # The newly calculated sha256 digest will be included in the metadata, so we need to revalidate
    for asset in asset_blob.assets.all():
        validate_asset_metadata.delay(asset.id)


@shared_task
@atomic
def write_manifest_files(version_id: int) -> None:
    version: Version = Version.objects.get(id=version_id)
    logger.info('Writing manifests for version %s:%s', version.dandiset.identifier, version.version)

    write_dandiset_yaml(version, logger=logger)
    write_assets_yaml(version, logger=logger)
    write_dandiset_jsonld(version, logger=logger)
    write_assets_jsonld(version, logger=logger)
    write_collection_jsonld(version, logger=logger)


def encode_pydantic_error(error) -> Dict[str, str]:
    return {'field': error['loc'][0], 'message': error['msg']}


def encode_jsonschema_error(error: jsonschema.exceptions.ValidationError) -> Dict[str, str]:
    return {'field': '.'.join([str(p) for p in error.path]), 'message': error.message}


def collect_validation_errors(
    error: dandischema.exceptions.ValidationError,
) -> List[Dict[str, str]]:
    validation_errors = []
    for error in error.errors:
        if type(error) is dict:
            # pydantic validation errors come in dicts
            validation_errors.append(encode_pydantic_error(error))
        else:
            # The jsonschema validation errors are wrapped in an extra list
            # We need to unwrap them before we can deal with them
            error = error[0]
            validation_errors.append(encode_jsonschema_error(error))
    return validation_errors


@shared_task
@atomic
# This method takes both a version_id and an asset_id because asset metadata renders differently
# depending on which version the asset belongs to.
def validate_asset_metadata(asset_id: int) -> None:
    logger.info('Validating asset metadata for asset %s', asset_id)
    asset: Asset = Asset.objects.get(id=asset_id)

    asset.status = Asset.Status.VALIDATING
    asset.save()

    try:
        metadata = asset.published_metadata()
        validate(metadata, schema_key='PublishedAsset', json_validation=True)
    except dandischema.exceptions.ValidationError as e:
        logger.error('Error while validating asset %s', asset_id)
        asset.status = Asset.Status.INVALID

        validation_errors = collect_validation_errors(e)
        asset.validation_errors = validation_errors
        asset.save()
        return
    except ValueError as e:
        # A bare ValueError is thrown when dandischema generates its own exceptions, like a
        # mismatched schemaVersion.
        asset.status = Asset.Status.INVALID
        asset.validation_errors = [{'field': '', 'message': str(e)}]
        asset.save()
        return
    logger.info('Successfully validated asset %s', asset_id)
    asset.status = Asset.Status.VALID
    asset.validation_errors = []
    asset.save()


@shared_task
@atomic
def validate_version_metadata(version_id: int) -> None:
    logger.info('Validating dandiset metadata for version %s', version_id)
    version: Version = Version.objects.get(id=version_id)

    version.status = Version.Status.VALIDATING
    version.save()

    try:
        publish_version = version.publish_version
        metadata = publish_version.metadata

        # Inject a dummy DOI so the metadata is valid
        metadata['doi'] = '10.80507/dandi.123456/0.123456.1234'

        validate(metadata, schema_key='PublishedDandiset', json_validation=True)
    except dandischema.exceptions.ValidationError as e:
        logger.error('Error while validating version %s', version_id)
        version.status = Version.Status.INVALID

        validation_errors = collect_validation_errors(e)
        version.validation_errors = validation_errors
        version.save()
        return
    except ValueError as e:
        # A bare ValueError is thrown when dandischema generates its own exceptions, like a
        # mismatched schemaVersion.
        version.status = Version.Status.INVALID
        version.validation_errors = [{'field': '', 'message': str(e)}]
        version.save()
        return
    logger.info('Successfully validated version %s', version_id)
    version.status = Version.Status.VALID
    version.validation_errors = []
    version.save()


@shared_task
def delete_doi_task(doi: str) -> None:
    delete_doi(doi)
