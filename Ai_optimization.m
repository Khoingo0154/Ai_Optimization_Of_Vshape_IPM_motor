%% CLEAN UP

close all;
clear;
clc;


%% PARAMETER LIMITS / SETUP

script_dir = fileparts(mfilename('fullpath'));

dataFile_name = "Ai_Optimization_Bounds.xlsx";
dataFilePath = fullfile(script_dir, dataFile_name);
data = readtable(dataFilePath, 'VariableNamingRule', 'preserve');

names = data.Parameter;
lLims = data.Lower_Limit;
uLims = data.Upper_Limit;
steps = data.Step;
units = data.Unit;

% Create parameter value table (Initial values I have been using)
% paramValues = [90, 1, 0.9, 1.5, 1.1899, 1.5, 18.07656, 2.1128, 6.90142, 10.88076, 5.4, 6, 3.5, 2, 2.4, 5.282, 31.80195, 10];
paramValuesFile_name = "Ai_Optimization_ParamValues.xlsx";
paramValuesFilePath = fullfile(script_dir, paramValuesFile_name);
paramValues = readtable(paramValuesFilePath, 'VariableNamingRule', 'preserve');


%% ESTALISH CONNECTION WITH ANSYS MAXWELL

% Start Ansys Electronics Desktop via ActiveX
iMaxwell = actxserver('Ansoft.ElectronicsDesktop');

% Access main desktop environment
oDesktop = invoke(iMaxwell, 'GetAppDesktop');
oDesktop.RestoreWindow; % Brings the Ansys window up so you can see it

% Get Maxwell project path
project_name = 'Matlab_Ai_Optimization';
full_project_file = fullfile(script_dir, project_name+".aedt");

% Open file. If open, grab active instead:
try
    oProject = invoke(oDesktop, 'SetActiveProject', project_name);
catch
    oProject = invoke(oDesktop, 'OpenProject', full_project_file);
end

% Set design handle
oDesign = invoke(oProject, 'SetActiveDesign', 'Vshape_IPM');


%% SIMULATION & EXPORTING DATA

% repeat for all designs
for iteration = 1:size(paramValues, 1)

    try
        invoke(oDesign, 'DeleteFullAllSolutions')
    catch
        
    end
    
    % Set variables
    for i = 1:length(names)
        if(units{i}~="")
            invoke(oDesign, 'SetVariableValue', names{i}, paramValues{iteration, i} + string(units{i}));
        else
            invoke(oDesign, 'SetVariableValue', names{i}, num2str(paramValues{iteration, i}));
        end
    end

    % Reset graphs
    oAnalysisModule = oDesign.GetModule('AnalysisSetup');
    invoke(oAnalysisModule, 'ResetSetupToTimeZero', 'Setup1');

    % Run simulation
    invoke(oDesign, 'Analyze', 'Setup1');
    
    % Export Data
    oModule = oDesign.GetModule('ReportSetup');
    output_csv_file = fullfile(script_dir, ['output_vars_iter_', num2str(iteration), '.csv']);
    invoke(oModule, 'ExportToFile', 'OutputVariablesTable', output_csv_file);
    
    % Read the table
    outputData = readtable(output_csv_file, 'VariableNamingRule', 'preserve');

end


%% CLOSE CONNECTION WITH ANSYS MAXWELL

%invoke(oProject, 'Save'); % <----- If we want to save project afterwards (right now i dont)
% oDesktop.CloseProject(project_name); %<----- If we want to close the project afterwards
delete(iMaxwell);
clear iMaxwell oDesktop oProject oDesign;