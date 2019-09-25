'''
    2019 Benjamin Kellenberger
'''

import importlib
import inspect
import json
from psycopg2 import sql
from celery import current_app
from kombu import Queue
from modules.AIWorker.backend.worker import functional
from modules.AIWorker.backend import fileserver
from modules.Database.app import Database
from util.helpers import get_class_executable


class AIWorker():

    def __init__(self, config, app):
        if config.getProperty('Project', 'demoMode', type=bool, fallback=False):    #TODO: project-specific?
            raise Exception('AIWorker cannot be launched in demo mode.')
        
        self.config = config
        self.dbConnector = Database(config)
        self._init_fileserver()
        self._init_project_queues()
            

    def _init_fileserver(self):
        '''
            The AIWorker has a special routine to detect whether the instance it is running on
            also hosts the file server. If it does, data are loaded directly from disk to avoid
            going through the loopback network.
        '''
        self.fileServer = fileserver.FileServer(self.config)


    def _init_project_queues(self):
        '''
            Queries the database for projects that support AIWorkers
            and adds respective queues to Celery to listen to them.
            TODO: implement custom project-to-AIWorker routing
        '''
        queues = list(current_app.conf.task_queues)
        print('Existing queues: {}'.format(', '.join([c.name for c in queues])))
        current_queue_names = set([c.name for c in queues])
        log_updates = []
        projects = self.dbConnector.execute('SELECT shortname FROM aide_admin.project WHERE ai_model_enabled = TRUE;', None, 'all')
        if len(projects):
            for project in projects:
                pName = project['shortname']
                if not pName in current_queue_names:
                    current_queue_names.add(pName)
                    queues.append(Queue(pName))
                    current_app.control.add_consumer(pName)
                    log_updates.append(pName)
            
            current_app.conf.update(task_queues=tuple(queues))
            print('Added queue(s) for project(s): {}'.format(', '.join(log_updates)))


    def _init_model_instance(self, project, modelLibrary, modelSettings):

        # try to parse model settings
        if modelSettings is not None and len(modelSettings):
            try:
                modelSettings = json.loads(modelSettings)
            except Exception as err:
                print('WARNING: could not read model options. Error message: {message}.'.format(
                    message=str(err)
                ))
                modelSettings = None
        else:
            modelSettings = None

        # import class object
        modelClass = get_class_executable(modelLibrary)

        # verify functions and arguments
        requiredFunctions = {
            '__init__' : ['project', 'config', 'dbConnector', 'fileServer', 'options'],
            'train' : ['stateDict', 'data', 'updateStateFun'],
            'average_model_states' : ['stateDicts', 'updateStateFun'],
            'inference' : ['stateDict', 'data', 'updateStateFun']
        }
        functionNames = [func for func in dir(modelClass) if callable(getattr(modelClass, func))]

        for key in requiredFunctions:
            if not key in functionNames:
                raise Exception('Class {} is missing required function {}.'.format(modelLibrary, key))

            # check function arguments bidirectionally
            funArgs = inspect.getargspec(getattr(modelClass, key))
            for arg in requiredFunctions[key]:
                if not arg in funArgs.args:
                    raise Exception('Method {} of class {} is missing required argument {}.'.format(modelLibrary, key, arg))
            for arg in funArgs.args:
                if arg != 'self' and not arg in requiredFunctions[key]:
                    raise Exception('Unsupported argument {} of method {} in class {}.'.format(arg, key, modelLibrary))

        # create AI model instance
        return modelClass(project=project,
                            config=self.config,
                            dbConnector=self.dbConnector,
                            fileServer=self.fileServer.get_secure_instance(project),
                            options=modelSettings)
        

    def _init_alCriterion_instance(self, project, alLibrary, alSettings):
        '''
            Creates the Active Learning (AL) criterion provider instance.
        '''
        # try to parse settings
        if alSettings is not None and len(alSettings):
            try:
                alSettings = json.loads(alSettings)
            except Exception as err:
                print('WARNING: could not read AL criterion options. Error message: {message}.'.format(
                    message=str(err)
                ))
                alSettings = None
        else:
            alSettings = None

        # import class object
        modelClass = get_class_executable(alLibrary)

        # verify functions and arguments
        requiredFunctions = {
            '__init__' : ['project', 'config', 'dbConnector', 'fileServer', 'options'],
            'rank' : ['data', 'updateStateFun']
        }
        functionNames = [func for func in dir(modelClass) if callable(getattr(modelClass, func))]

        for key in requiredFunctions:
            if not key in functionNames:
                raise Exception('Class {} is missing required function {}.'.format(alLibrary, key))

            # check function arguments bidirectionally
            funArgs = inspect.getargspec(getattr(modelClass, key))
            for arg in requiredFunctions[key]:
                if not arg in funArgs.args:
                    raise Exception('Method {} of class {} is missing required argument {}.'.format(alLibrary, key, arg))
            for arg in funArgs.args:
                if arg != 'self' and not arg in requiredFunctions[key]:
                    raise Exception('Unsupported argument {} of method {} in class {}.'.format(arg, key, alLibrary))

        # create AI model instance
        return modelClass(project=project,
                            config=self.config,
                            dbConnector=self.dbConnector,
                            fileServer=self.fileServer.get_secure_instance(project),
                            options=alSettings)


    def _get_model_instance(self, project):
        '''
            Returns the class instance of the model specified in the given
            project.
            TODO: cache models?
        '''
        # get model settings for project
        queryStr = '''
            SELECT ai_model_library, ai_model_settings FROM aide_admin.project
            WHERE shortname = %s;
        '''
        result = self.dbConnector.execute(queryStr, (project,), 1)
        modelLibrary = result[0]['ai_model_library']
        modelSettings = result[0]['ai_model_settings']

        # create new model instance
        modelInstance = self._init_model_instance(project, modelLibrary, modelSettings)

        return modelInstance


    def _get_alCriterion_instance(self, project):
        '''
            Returns the class instance of the Active Learning model
            specified in the project.
            TODO: cache models?
        '''
        # get model settings for project
        queryStr = '''
            SELECT ai_alCriterion_library, ai_alCriterion_settings FROM aide_admin.project
            WHERE shortname = %s;
        '''
        result = self.dbConnector.execute(queryStr, (project,), 1)
        modelLibrary = result[0]['ai_alcriterion_library']
        modelSettings = result[0]['ai_alcriterion_settings']

        # create new model instance
        modelInstance = self._init_alCriterion_instance(project, modelLibrary, modelSettings)

        return modelInstance



    def aide_internal_notify(self, message):
        '''
            Used for AIde administrative communication between AIController
            and AIWorker(s), e.g. for setting up queues.
        '''
        if 'task' in message:
            if message['task'] == 'add_projects':
                self._init_project_queues()



    def call_train(self, project, data, subset):

        # get project-specific model
        modelInstance = self._get_model_instance(project)

        return functional._call_train(project, data, subset, getattr(modelInstance, 'train'),
                self.dbConnector, self.fileServer, self.config)
    


    def call_average_model_states(self, project):

        # get project-specific model
        modelInstance = self._get_model_instance(project)

        return functional._call_average_model_states(project, getattr(modelInstance, 'average_model_states'),
                self.dbConnector, self.fileServer, self.config)



    def call_inference(self, project, imageIDs):

        # #TODO
        # from celery.contrib import rdb
        # rdb.set_trace()
        # get project-specific model and AL criterion
        modelInstance = self._get_model_instance(project)
        alCriterionInstance = self._get_alCriterion_instance(project)

        return functional._call_inference(project, imageIDs,
                getattr(modelInstance, 'inference'),
                getattr(alCriterionInstance, 'rank'),
                self.dbConnector, self.fileServer, self.config)