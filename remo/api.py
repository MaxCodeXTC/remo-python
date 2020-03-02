import http
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import filetype
import requests
from urllib.parse import quote

from .endpoints import backend
from .utils import FileResolver


class UploadStatus:
    def __init__(self, total_count):
        self.total_count = total_count
        self.current_count = 0
        self.start = datetime.now()
        self.reported_progress = 0

    def update(self, count):
        self.current_count += count

    def progress(self):
        percentage = int(self.current_count / self.total_count * 100)
        if percentage > self.reported_progress:
            elapsed = (datetime.now() - self.start).seconds + 1e-3
            avg_speed = self.current_count / elapsed
            eta = timedelta(seconds=(self.total_count - self.current_count) / avg_speed)
            print(
                'Progress {}% - {}/{} - elapsed {} - speed: {} img / s, ETA: {}'.format(
                    percentage,
                    self.current_count,
                    self.total_count,
                    timedelta(seconds=elapsed),
                    "%.2f" % avg_speed,
                    eta,
                )
            )
        self.reported_progress = percentage


class BaseAPI:
    def __init__(self, server, email, password):
        self.server = server
        self.token = None
        self._login(email, password)

    def _login(self, email, password):
        try:
            resp = requests.post(self.url(backend.login), data={"password": password, "email": email})
        except requests.exceptions.ConnectionError:
            print('ERROR: Failed connect to server')
            return

        if resp.status_code != http.HTTPStatus.OK:
            raise Exception(resp.json())

        self.token = resp.json().get('key')

    def _is_authenticated(self):
        return self.token is not None

    def _auth_header(self):
        if not self._is_authenticated():
            raise Exception('Not authenticated')
        return {'Authorization': 'Token {}'.format(self.token)}

    def url(self, endpoint, *args, **kwargs):
        return self._build_url(self.server, endpoint, *args, **kwargs)

    def post(self, *args, **kwargs):
        return requests.post(*args, headers=self._auth_header(), **kwargs)

    def get(self, *args, **kwargs):
        return requests.get(*args, headers=self._auth_header(), **kwargs)

    @staticmethod
    def _build_url(*args, **kwargs):
        """
        Builds full url from inputs.
        Additional param `tail_slash` specifies tailed slash

        :param args: typically server and endpoint
        :param kwargs: additional query parameters
        :return: full url
        """
        args = list(filter(lambda arg: arg is not None, args))
        tail_slash = kwargs.pop('tail_slash', (str(args[-1])[-1] == '/'))
        url = '/'.join(map(lambda x: str(x).strip('/'), args))

        params = []
        for key, val in kwargs.items():
            if type(val) is str:
                if ',' in val:
                    values = val.split(',')
                    values = ','.join(map(quote, values))
                    params.append('{}={}'.format(key, values))
                else:
                    params.append('{}={}'.format(key, quote(val)))
            elif type(val) is list:
                values = ','.join(map(lambda v: quote(str(v)), val))
                params.append('{}={}'.format(key, values))
            elif val:
                params.append('{}={}'.format(key, val))

        if len(params):
            joined_params = "&".join(params)
            separator = '&' if '?' in url else '/?'
            url = "{}{}{}".format(url, separator, joined_params)
        elif '?' not in url and tail_slash:
            url += '/'

        return url


class API(BaseAPI):
    def create_dataset(self, name):
        return self.post(self.url(backend.dataset), json={"name": name}).json()

    def create_annotation_set(self, annotation_task, dataset_id, name, classes=[]):
        """
        Creates a new empty annotation set
        Args:
            - annotation_task: str.
                name of the annotation task
            - dataset_id: int.
                the id of the dataset to create new annotation set
            - name: str.
                Name of the annotation set to create.
            - classes: list of str.
                list of classes.
                Example: ['Cat', 'Dog']
        """

        payload = {
            "annotation_task": annotation_task,
            "classes": classes,
            "dataset_id": dataset_id,
            "name": name,
        }

        return self.post(self.url(backend.v1_create_annotation_set), json=payload).json()

    def add_annotation(
        self, dataset_id, annotation_set_id, image_id, existing_annotations=None, classes=None, objects=None
    ):

        url = self.url(backend.add_annotation).format(dataset_id, annotation_set_id, image_id)
        existing_annotations = existing_annotations if existing_annotations else []

        payload = {}
        if objects:
            # It's object detection
            payload = {"objects": existing_annotations + objects}
        elif classes:
            # It's classification
            payload = {"classes": existing_annotations + classes}

        if payload:
            return self.post(url, json=payload).json()

    def upload_file(self, dataset_id, path, annotation_task=None, folder_id=None):
        name = os.path.basename(path)
        files = {'files': (name, open(path, 'rb'), filetype.guess_mime(path))}
        data = {}
        if annotation_task:
            data['annotation_task'] = annotation_task

        url = self.url(backend.dataset_upload.format(dataset_id), folder_id=folder_id)
        return self.post(url, files=files, data=data).json()

    # TODO: fix progress to include both local files and uploads
    def upload_files(
        self,
        dataset_id,
        files_to_upload=[],
        annotation_task=None,
        folder_id=None,
        status=None,
        annotation_set_id=None,
        class_encoding=None,
    ):
        files = [
            ('files', (os.path.basename(path), open(path, 'rb'), filetype.guess_mime(path)))
            for path in files_to_upload
        ]
        data = {}
        if annotation_task:
            data['annotation_task'] = annotation_task

        if isinstance(class_encoding, dict):
            for key, val in class_encoding.items():
                data['class_encoding_{}'.format(key)] = val

        url = self.url(
            backend.dataset_upload.format(dataset_id),
            folder_id=folder_id,
            annotation_set_id=annotation_set_id,
        )
        r = self.post(url, files=files, data=data)

        if r.status_code != http.HTTPStatus.OK:
            print('Possible Error - Response:', r.text, 'files:', files_to_upload)

        status.update(len(files))
        status.progress()
        return r.json()

    # TODO: fix progress to include both local files and uploads
    def bulk_upload_files(
        self,
        dataset_id,
        files_to_upload,
        annotation_task=None,
        folder_id=None,
        annotation_set_id=None,
        class_encoding=None,
    ):

        # files to upload
        files = FileResolver(files_to_upload, annotation_task is not None).resolve()
        groups = self.split_files_by_size(files)
        status = UploadStatus(len(files))
        with ThreadPoolExecutor(1) as ex:
            res = ex.map(
                lambda bulk: self.upload_files(
                    dataset_id, bulk, annotation_task, folder_id, status, annotation_set_id, class_encoding
                ),
                groups,
            )

        results = res
        return results

    def chunks(self, my_list, chunk_size=2000):
        groups = []
        """Yield successive n-sized chunks from l."""
        for i in range(0, len(my_list), chunk_size):
            groups.append(my_list[i : i + chunk_size])

        return groups

    def split_files_by_size(self, files):
        groups = []
        bulk = []
        total_size = 0
        bulk_size = 8 * 1024 * 1024
        for path in files:
            total_size += os.path.getsize(path)
            bulk.append(path)
            if total_size >= bulk_size:
                groups.append(bulk)
                bulk = []
                total_size = 0

        if len(bulk):
            groups.append(bulk)
        return groups

    def upload_local_files(
        self,
        dataset_id,
        local_files,
        annotation_task=None,
        folder_id=None,
        annotation_set_id=None,
        class_encoding=None,
    ):
        payload = {"local_files": local_files}
        if annotation_task:
            payload['annotation_task'] = annotation_task
        if folder_id:
            payload['folder_id'] = folder_id
        if isinstance(class_encoding, dict):
            payload['class_encoding'] = class_encoding

        url = self.url(backend.dataset_upload.format(dataset_id), annotation_set_id=annotation_set_id)
        return self.post(url, json=payload).json()

    def upload_urls(
        self,
        dataset_id,
        urls,
        annotation_task=None,
        folder_id=None,
        annotation_set_id=None,
        class_encoding=None,
    ):
        payload = {"urls": urls}
        if annotation_task:
            payload['annotation_task'] = annotation_task
        if folder_id:
            payload['folder_id'] = folder_id
        if isinstance(class_encoding, dict):
            payload['class_encoding'] = class_encoding

        url = self.url(backend.dataset_upload.format(dataset_id), annotation_set_id=annotation_set_id)
        return self.post(url, json=payload).json()

    def list_datasets(self):
        url = self.url(backend.v1_datasets)
        return self.get(url).json()

    def list_dataset_images(self, dataset_id, limit=None, offset=None):
        url = self.url(backend.v1_sdk_dataset_images.format(dataset_id), limit=limit, offset=offset)
        return self.get(url).json()

    def list_dataset_contents(self, dataset_id, limit=None):
        url = self.url(backend.v1_dataset_images.format(dataset_id), limit=limit)
        return self.get(url).json()

    def list_dataset_contents_by_folder(self, dataset_id, folder_id, limit=None):
        url = self.url(backend.dataset_folder_content.format(dataset_id, folder_id), limit=limit)
        return self.get(url).json()

    def get_dataset(self, id):
        url = self.url(backend.v1_datasets, id, tail_slash=True)
        return self.get(url).json()

    def list_annotation_sets(self, dataset_id):
        url = self.url(backend.v1_dataset_annotation_sets.format(dataset_id))
        return self.get(url).json()

    def get_annotation_set(self, id):
        url = self.url(backend.v1_annotation_set.format(id))
        return self.get(url).json()

    def export_annotations(
        self, annotation_set_id: int, annotation_format='json', export_coordinates='pixel', full_path='true'
    ) -> bytes:
        """
        Exports annotations in given format

        Args:
            annotation_set_id: annotation set id
            annotation_format: can be one of ['json', 'coco', 'csv'], default='json'
            full_path: uses full image path (e.g. local path), can be one of ['true', 'false'], default='false'
            export_coordinates: converts output values to percentage or pixels, can be one of ['pixel', 'percent'], default='pixel'

        Returns:
            annotation file content
        """
        url = self.url(
            backend.v1_export_annotations.format(annotation_set_id),
            annotation_format=annotation_format,
            export_coordinates=export_coordinates,
            full_path=full_path,
        )
        return self.get(url).content

    def get_annotation_info(self, dataset_id, annotation_set_id, image_id):
        """
        Args:
            dataset_id: dataset id
            annotation_set_id: annotation set id
            image_id: image id

        Returns: annotations info
        """
        url = self.url(backend.annotation_info.format(dataset_id, annotation_set_id, image_id))
        return self.get(url).json()

    def list_annotation_set_classes(self, annotation_set_id: int):
        """
        Lists annotation set classes

        Args:
            annotation_set_id: int.

        Returns: list of classes
        """
        url = self.url(backend.annotation_set.format(annotation_set_id))
        json_data = self.get(url).json()
        return json_data.get('classes', [])

    def get_image_content(self, url) -> bytes:
        return self.get(self.url(url)).content

    def get_image(self, image_id):
        url = self.url(backend.v1_sdk_images, image_id, tail_slash=True)
        return self.get(url).json()

    def search_images(self, classes=None, task=None, dataset_id=None, limit=None):
        """
        Search images given a list of classes and tasks
        Args:
            classes: string or list of strings - search for images which match all given classes
            task: string - annotation task
            dataset_id: int - performs search in given dataset otherwise in all datasets
            limit: int - limits search result
        Returns: dictionary of image_id, dataset_id, name, preview, annotations, classes, dimensions
        """
        # TODO: adding task names on return
        params = {}
        if task:
            params['tasks'] = task
        if classes:
            params['classes'] = classes
        if dataset_id:
            params['dataset_id'] = dataset_id
        if limit:
            params['limit'] = limit

        url = self.url(backend.v1_search, **params)
        return self.get(url).json()['results']
